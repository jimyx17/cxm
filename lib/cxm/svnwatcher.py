# -*- coding:Utf-8 -*-

# cxm - Clustered Xen Management API and tools
# Copyleft 2011-2012 - Nicolas AGIUS <nicolas.agius@lps-it.fr>
# $Id:$

###########################################################################
#
# This file is part of cxm.
#
# cxm is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###########################################################################

from twisted.internet import protocol, reactor, threads
from twisted.application.service import Service
from twisted.internet.base import DelayedCall

from copy import deepcopy

from agent import Agent
from xencluster import XenCluster
import node, core
import logs as log

class InotifyPP(protocol.ProcessProtocol):
	
	# List of filenames ignored by commit
	blacklist=['tempfile.tmp']

	def __init__(self, node, agent=None):
		self.toAdd=list()
		self.toDel=list()
		self._call=None
		self.node=node
		self.agent=agent

		if self.agent:
			# Short delay to quickly propagate modifications to others nodes
			self.delay=0.5
		else:
			# We wait more longer before autocommit if we are standalone
			self.delay=10

	def connectionMade(self):
		log.info("Inotify started.")

	def outReceived(self, data):
		for line in data.split('\n'):
			if len(line) <= 0:
				continue
			info=line.split()
			if info[2] in self.blacklist:
				continue
			if info[1] == "CREATE":
				self.toAdd.append(info[2])
			elif info[1] == "DELETE":
				self.toDel.append(info[2])
		
		# Don't commit if there is no files
		if len(self.toAdd) <= 0 and len(self.toDel) <= 0:
			return

		if isinstance(self._call, DelayedCall) and self._call.active():
			self._call.reset(self.delay)
		else:
			self._call=reactor.callLater(self.delay, self.doCommit)

	def doCommit(self):
		toAdd=deepcopy(self.toAdd)
		self.toAdd=list()
		toDel=deepcopy(self.toDel)
		self.toDel=list()

		log.info("Committing for "+", ".join(toAdd)+", ".join(toDel))
		try:
			if len(toAdd) > 0:
				self.node.run("svn add " + " ".join(map(lambda x: core.cfg['VMCONF_DIR']+x, toAdd)))
			if len(toDel) > 0:
				self.node.run("svn delete " + " ".join(map(lambda x: core.cfg['VMCONF_DIR']+x, toDel)))
			self.node.run("svn --non-interactive commit -m 'svnwatcher autocommit' "+core.cfg['VMCONF_DIR'])
			self.doUpdate()
		except Exception, e:
			log.err("SVN failed: %s" % (e))
			reactor.stop()
		
	def doUpdate(self):
		def doClusterUpdate(result):
			for node in result.get_nodes():
				d=threads.deferToThread(node.run, "svn update "+ core.cfg['VMCONF_DIR'])
				d.addErrback(log.err)

		def getCluster(result):
			d=XenCluster.getDeferInstance(result)
			d.addCallback(doClusterUpdate)
			d.addErrback(log.err)
			return d

		if self.agent:
			d=self.agent.getNodesList()
			d.addCallback(getCluster)
			d.addErrback(log.err)
			return d
		else:
			self.node.run("svn update "+ core.cfg['VMCONF_DIR'])
			
	def processEnded(self, reason):
		log.warn("Inotify has died: %s" % (reason.value))
		try:
			reactor.stop()
		except:
			pass


class SvnwatcherService(Service):

	def __init__(self):
		self.node=node.Node.getLocalInstance() # Local node, always connected
		self.agent=Agent()		# Factory will be stopped if cxmd does'nt respond

	def startService(self):
		def standalone(reason):
			log.info("Starting in standalone mode.")
			self.agent=None
			
		def cluster(result):
			log.info("Starting in cluster mode.")

		Service.startService(self)

		msg=self.node.run("svn status "+core.cfg['VMCONF_DIR'] +" 2>&1").read()
		if len(msg)>0:
			log.err("Your repo is not clean. Please check it : %s" % (msg))
			raise Exception("SVN repo not clean")

		d=self.agent.ping()
		d.addCallbacks(cluster, standalone)
		d.addBoth(lambda _: self.spawnInotify())
		d.addErrback(log.err)
		return d
	
	def stopService(self):
		if self.running:
			Service.stopService(self)

			try:
				self._process.signalProcess('TERM')
			except:
				pass

			try:
				reactor.stop()
			except:
				pass

	def forceUpdate(self):
		log.info("SIGHUP received: updating all repos.")
		try:
			self.pp.doUpdate()
		except:
			pass

	def spawnInotify(self):
		# We use this ugly way because Pyinotify and Twisted's INotify require Python 2.6
		argv=["inotifywait", "-e", "create", "-e", "modify", "-e", "delete", "-m", core.cfg['VMCONF_DIR'], "--exclude", "/\.|~$|[0-9]+$"]
		self.pp = InotifyPP(self.node, self.agent)
		self._process=reactor.spawnProcess(self.pp, argv[0], argv, {})


# vim: ts=4:sw=4:ai
