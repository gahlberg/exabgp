# encoding: utf-8
"""
store.py

Created by Thomas Mangin on 2009-11-05.
Copyright (c) 2009-2015 Exa Networks. All rights reserved.
"""

from exabgp.bgp.message import OUT
from exabgp.bgp.message import Update
from exabgp.bgp.message.refresh import RouteRefresh
from exabgp.bgp.message.update.attribute import Attributes

from exabgp.rib.cache import Cache

# XXX: FIXME: we would not have to use so many setdefault if we pre-filled the dicts with the families


class OutgoingRIB (Cache):
	def __init__ (self, families):
		Cache.__init__(self,families)

		self._watchdog = {}
		self.cache = False
		self.families = families

		self._new_nlri = {}          # self._new_nlri[nlri-index] = change
		self._new_attr_af_nlri = {}  # self._new_attr_af_nlri[attr-index][family][nlri-index] = change
		self._new_attribute = {}     # self._new_attribute[attr-index] = attributes

		# _new_nlri: we are modifying this nlri
		# this is useful to iterate and find nlri currently handled

		# _new_attr_af_nlri: add or remove the nlri
		# this is the best way to iterate over NLRI when generating updates
		# sharing attributes, then family

		# _new_attribute: attributes of one of the changes
		# makes our life easier, but could be removed

		self._enhanced_refresh_start = []
		self._enhanced_refresh_delay = []

		self.reset()

	# will resend all the routes once we reconnect
	def reset (self):
		# WARNING : this function can run while we are in the updates() loop too !
		self._enhanced_refresh_start = []
		self._enhanced_refresh_delay = []
		for _ in self.updates(True):
			pass

	# back to square one, all the routes are removed
	def clear (self):
		self.clear_cache()
		self._new_nlri = {}
		self._new_attr_af_nlri = {}
		self._new_attribute = {}
		self.reset()

	def resend (self, families, enhanced_refresh):
		# families can be None or []
		requested_families = self.families if not families else set(families).intersection(self.families)

		if enhanced_refresh:
			for family in requested_families:
				if family not in self._enhanced_refresh_start:
					self._enhanced_refresh_start.append(family)

		for change in self.cached_changes(requested_families):
			self.add_to_rib(change,True)

	def queued_changes (self):
		for change in self._new_nlri.values():
			yield change

	def replace (self, previous, changes):
		for change in previous:
			change.nlri.action = OUT.WITHDRAW
			self.add_to_rib(change,True)

		for change in changes:
			self.add_to_rib(change,True)

	def add_to_rib_watchdog (self, change):
		watchdog = change.attributes.watchdog()
		withdraw = change.attributes.withdraw()
		if watchdog:
			if withdraw:
				self._watchdog.setdefault(watchdog,{}).setdefault('-',{})[change.index()] = change
				return True
			self._watchdog.setdefault(watchdog,{}).setdefault('+',{})[change.index()] = change
		self.add_to_rib(change)
		return True

	def announce_watchdog (self, watchdog):
		if watchdog in self._watchdog:
			for change in self._watchdog[watchdog].get('-',{}).values():
				change.nlri.action = OUT.ANNOUNCE  # pylint: disable=E1101
				self.add_to_rib(change)
				self._watchdog[watchdog].setdefault('+',{})[change.index()] = change
				self._watchdog[watchdog]['-'].pop(change.index())

	def withdraw_watchdog (self, watchdog):
		if watchdog in self._watchdog:
			for change in self._watchdog[watchdog].get('+',{}).values():
				change.nlri.action = OUT.WITHDRAW
				self.add_to_rib(change)
				self._watchdog[watchdog].setdefault('-',{})[change.index()] = change
				self._watchdog[watchdog]['+'].pop(change.index())

	def add_to_rib (self, change, force=False):
		# WARNING: do not call change.nlri.index as it does not prepend the family
		# WARNING : this function can run while we are in the updates() loop

		# import traceback
		# traceback.print_stack()
		# print "inserting", change.extensive()

		if not force and self._enhanced_refresh_start:
			self._enhanced_refresh_delay.append(change)
			return

		change_nlri_index = change.index()
		change_family = change.nlri.family()
		change_attr_index = change.attributes.index()

		attr_af_nlri = self._new_attr_af_nlri
		new_nlri = self._new_nlri
		new_attr = self._new_attribute

		# removing a route before we had time to announce it ?
		if change_nlri_index in new_nlri:
			# pop removes the entry
			old_change = new_nlri.pop(change_nlri_index)
			old_attr_index = old_change.attributes.index()
			# do not delete new_attr, other routes may use it
			del attr_af_nlri[old_attr_index][change_family][change_nlri_index]
			# do not delete the rest of the dict tree as:
			#  we may have to recreate it otherwise
			#  it will be deleted once used anyway
			#  we have to check for empty data in the updates() loop (so why do it twice!)

			# if we cache sent NLRI and this NLRI was never sent before, we do not need to send a withdrawal
			# as the route removed before we could announce it
			if self.is_cached(change):
				if old_change.nlri.action == OUT.ANNOUNCE and change.nlri.action == OUT.WITHDRAW:
					return

		if not force and self.in_cache(change):
			return

		# add the route to the list to be announced
		attr_af_nlri.setdefault(change_attr_index,{}).setdefault(change_family,{})[change_nlri_index] = change
		new_nlri[change_nlri_index] = change

		if change_attr_index not in new_attr:
			new_attr[change_attr_index] = change.attributes

	def updates (self, grouped):
		# if we need to perform a route-refresh, sending the message
		# to indicate the start of the announcements

		rr_announced = []

		for afi,safi in self._enhanced_refresh_start:
			rr_announced.append((afi,safi))
			yield Update(RouteRefresh(afi,safi,RouteRefresh.start),Attributes())

		# generating Updates from what is in the RIB

		attr_af_nlri = self._new_attr_af_nlri
		new_attr = self._new_attribute

		for attr_index,per_family in attr_af_nlri.items():
			for family, changes in per_family.items():
				if not changes:
					continue

				# only yield once we have a consistent state, otherwise it will go wrong
				# as we will try to modify things we are iterating over and using

				attributes = new_attr[attr_index]

				if grouped:
					yield Update([change.nlri for change in changes.values()], attributes)
					for change in changes.values():
						self.update_cache(change)
				else:
					for change in changes.values():
						yield Update([change.nlri,], attributes)
						self.update_cache(change)

		# Update were send, clear the data we used

		self._new_nlri = {}
		self._new_attr_af_nlri = {}
		self._new_attribute = {}

		# If we are performing a route-refresh, indicating that the
		# update were all sent

		if rr_announced:
			for afi,safi in rr_announced:
				self._enhanced_refresh_start.remove((afi,safi))
				yield Update(RouteRefresh(afi,safi,RouteRefresh.end),Attributes())

			for change in self._enhanced_refresh_delay:
				self.add_to_rib(change,True)
			self._enhanced_refresh_delay = []

			for update in self.updates(grouped):
				yield update