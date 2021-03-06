# -*- coding:Utf-8 -*-

# cxm - Clustered Xen Management API and tools
# Copyleft 2010-2012 - Nicolas AGIUS <nicolas.agius@lps-it.fr>
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

"""This module hold the Node class."""

import paramiko, re, time, popen2, socket, StringIO, sys, os
from xen.xm import XenAPI
from xen.xm import main
from xen.util.xmlrpcclient import ServerProxy
from sets import Set

from metrics import Metrics
from vm import VM
import logs as log
import core, datacache


class Node:
	
	"""This class is used to perform action on a node within the xen cluster."""

	def __init__(self,hostname):
		"""Instanciate a Node object.

		This constructor open SSH and XenAPI connections to the node.
		If the node is not online, this will fail with an uncatched exception from paramiko or XenAPI.
		"""
		log.info("Connecting to", hostname, "...")
		self.hostname=hostname

		# Open SSH channel (localhost use popen2)
		if not self.is_local_node():
			self.ssh = paramiko.SSHClient()
			self.ssh.load_system_host_keys()
			#self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
			self.ssh.connect(hostname,22,'root', timeout=2)

		# Open Xen-API Session 
		if self.is_local_node():
			# Use unix socket on localhost
			self.server = XenAPI.Session("httpu:///var/run/xend/xen-api.sock")
			log.debug("[API]","Using unix socket.")
		else:
			self.server = XenAPI.Session("http://"+hostname+":9363")
			log.debug("[API]","Using tcp socket.")
		self.server.login_with_password("root", "")

		# Prepare connection with legacy API
		self.__legacy_server=None

		# Prepare metrics
		self.__metrics=None

		# Prepare cache
		self._cache=datacache.DataCache()
		self._last_refresh=0

	@staticmethod
	def getLocalInstance():
		"""Instanciate and return the Node object representing the local machine."""
		return Node(socket.gethostname())

	def disconnect(self):
		"""Close all connections."""
		# Close SSH
		try:
			self.ssh.close()
		except:
			pass

		# Close Xen-API
		try:
			self.server.xenapi.session.logout()
		except:
			pass

	def get_legacy_server(self):
		"""Return the legacy API socket."""
		if self.__legacy_server is None:
			if self.is_local_node():
				self.__legacy_server=ServerProxy("httpu:///var/run/xend/xmlrpc.sock")
				log.debug("[Legacy-API]","Using unix socket.")
			else:
				self.__legacy_server=ServerProxy("http://"+self.hostname+":8006")
				log.debug("[Legacy-API]","Using tcp socket.")
		return self.__legacy_server

	def get_metrics(self):
		"""Return the metrics instance of this node."""
		if self.__metrics is None:
			self.__metrics=Metrics(self)
		return self.__metrics

	def __repr__(self):
		return "<Node Instance: "+ self.hostname +">"

	def run(self,cmd):
		"""Execute command on this node via SSH (or via shell if this is the local node)."""
# Does'nt work with LVM commands
#		if(self.is_local_node()):
#			p = subprocess.Popen(cmd,shell=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
#			msg=p.stderr.read()
#			if(len(msg)>0):
#				raise ClusterNodeError(self.hostname,ClusterNodeError.SHELL_ERROR,msg)
#			return p.stdout
#		else:
		
# Deadlock bug if cmd's output is bigger than 65k
#       if(self.is_local_node()):
#           if core.cfg['DEBUG'] : print "DEBUG SHELL: "+ self.get_hostname() +" -> "+cmd
#           stdout, stdin, stderr = popen2.popen3(cmd,9300000)
#           msg=stderr.read()
#           if(len(msg)>0):
#               raise ClusterNodeError(self.hostname,ClusterNodeError.SHELL_ERROR,msg)

		if(core.cfg['PATH']):
			cmd=core.cfg['PATH'] + "/" + cmd

		if(self.is_local_node()):
			log.debug("[SHL]", self.hostname, "->", cmd)

			# Create buffers
			stdout=StringIO.StringIO()
			stderr=StringIO.StringIO()

			proc=popen2.Popen3(cmd, True) # Run cmd

			# Load output in the buffers and rewind them
			try:
				stdout.write(proc.fromchild.read())
			except IOError:
				pass # Discard broken pipe for backgrounded commands
			stdout.seek(0)

			try:
				stderr.write(proc.childerr.read())
			except IOError:
				pass
			stderr.seek(0)

			exitcode=proc.wait()
			if exitcode != 0:
				msg=stderr.read()
				raise ShellError(self.hostname,msg, exitcode >> 8)
		else:
			log.debug("[SSH]", self.hostname, "->", cmd)
			stdin, stdout, stderr = self.ssh.exec_command(cmd)
			# Lock bug workaround : Check exit status before trying to read stderr
			# Because sometimes, when stdout is big (maybe >65k ?), strderr.read() hand on
			# a thread's deadlock if stderr is readed before stdout...
			exitcode=stderr.channel.recv_exit_status()
			if exitcode != 0:
				stderr.channel.settimeout(3)
				try:
					msg=stderr.read()
					raise SSHError(self.hostname, msg, exitcode)
				except socket.timeout:
					raise SSHError(self.hostname, "Timeout reading stderr !", exitcode)
		return stdout

	def is_local_node(self):
		"""Return True if this node is the local node."""
		return socket.gethostname()==self.hostname
		
	def is_vm_started(self, vmname):
		"""Return True if the specified vm is started on this node."""
		vm=self.server.xenapi.VM.get_by_name_label(vmname)
		log.debug("[API]", self.hostname, "vm=", vm)
		try:
			return self.server.xenapi.VM.get_power_state(vm[0]) != "Halted"
		except IndexError:
			return False

	def is_vm_autostart_enabled(self, vmname):
		"""Return True if the autostart link is present for the specified vm on this node."""
		for link in self.run("ls /etc/xen/auto/").readlines():
			if vmname == link.strip():
				return True
		return False

	def get_hostname(self):
		"""Return the hostname of this node."""
		return self.hostname

	def get_bridges(self):
		"""Return the list of bridges on this node."""
		# brctl show | tail -n +2 | perl -ne 'print "$1\n" if(/^(\w+)\s/)'
		# ou 
		# find /sys/class/net/ -name bridge | grep -v brport | awk -F/ '{ print $5 }'
		# ou 
		# brctl show | perl -ne 'next if(/bridge/); print "$1\n" if(/^(\w+)\s/)'
		# ou
		# find /sys/class/net/ -maxdepth 2 -name bridge  |  awk -F/ '{ print $5 }'
		bridges=list()
		for line in self.run("find /sys/class/net/ -maxdepth 2 -name bridge").readlines():
			bridges.append(line.split('/')[4])
		return bridges

	def get_vlans(self):
		"""Return the list of vlans configured on this node."""
		vlans=list()
		for line in self.run("cat /proc/net/vlan/config | tail -n +3").readlines():
			vlans.append(line.split()[0])
		return vlans

	def get_vm_started(self, nocache=False):
		"""
		Return the number of started vm on this node.
		Result will be cached for 5 seconds, unless 'nocache' is True.
		"""

		return len(self.get_vms_names(nocache))

	def get_vgs(self,lvs):
		"""Return the list of volumes groups associated with the given logicals volumes."""
		vgs=list()
		for line in self.run("lvdisplay -c " + " ".join(lvs)).readlines():
			vgs.append(line.split(':')[1])

		return list(set(vgs))	# Delete duplicate entries
	
	def get_vgs_map(self, nocache=False):
		"""
		Return the dict of volumes groups with each associated physicals volumes of this node.
		Result will be cached for 1 hour, unless 'nocache' is True.
		"""

		def _get_vgs_map():
			map=dict()
			for line in self.run("pvs -o pv_name,vg_name --noheading").readlines():
				(pv, vg)=line.split()
				if pv.startswith("/dev/"):
					pv=pv[5:] # Delete '/dev/' prefix, if any

				map.setdefault(vg, []).append(pv)

			return map

		return self._cache.cache(3600, nocache, _get_vgs_map)

	def refresh_lvm(self,vgs):
		"""Perform a LVM refresh."""

		GRACE_TIME=60  # Don't refresh for 60 seconds

		if not core.cfg['NOREFRESH']:
			if self._last_refresh+GRACE_TIME < int(time.time()):
				self.run("lvchange --refresh " + " ".join(vgs))
				self._last_refresh=int(time.time())

	def deactivate_lv(self,vmname):
		"""Deactivate the logicals volumes of the specified VM on this node.

		Raise a RunningVmError if the VM is running.
		"""
		if(self.is_vm_started(vmname)):
			raise RunningVmError(self.hostname,vmname) 
		else:
			lvs=VM(vmname).get_lvs()
			if lvs:
				self.refresh_lvm(self.get_vgs(lvs))
				self.run("lvchange -aln " + " ".join(lvs))

	def deactivate_all_lv(self):
		"""Deactivate all the logicals volumes used by stopped VM on this node."""
		for vm in self.get_possible_vm_names():
			if not self.is_vm_started(vm):
				self.deactivate_lv(vm)

	def activate_lv(self,vmname):
		"""Activate the logicals volumes of the specified VM on this node."""
		lvs=VM(vmname).get_lvs()
		if lvs:
			self.refresh_lvm(self.get_vgs(lvs))
			self.run("lvchange -aly " + " ".join(lvs))
		
	def start(self, vmname):
		"""Start the specified VM on this node.

		vmname - (String) VM hostname 
		"""

		args = [core.cfg['VMCONF_DIR'] + vmname]

		# Use Legacy XMLRPC because Xen-API is sometimes buggy
		main.server=self.get_legacy_server()
		main.serverType=main.SERVER_LEGACY_XMLRPC
		main.xm_importcommand("create" , args)

		# Stupid bug : does'nt work with a bridge named xenbr2010 ...
		#args.append('--skipdtd') # Because file /usr/share/xen/create.dtd is missing
		#main.server=self.server
		#main.serverType=main.SERVER_XEN_API
		#main.xm_importcommand("create" , args)

	def reboot(self, vmname):
		"""Reboot the specified VM on this node.

		vmname - (String) VM hostname 
		Raise a NotRunningVmError if the vm is not running.
		"""

		try:
			vm=self.server.xenapi.VM.get_by_name_label(vmname)[0]
		except IndexError:
			raise NotRunningVmError(self.get_hostname(),vmname)
		self.server.xenapi.VM.clean_reboot(vm)

	def migrate(self, vmname, dest_node):
		"""Live migration of specified VM to the given node.

		Raise a NotRunningVmError if the vm is not started on this node.
		"""
#		if self.is_local_node():
#			# Use Legacy XMLRPC because Xen-API is sometimes buggy
#			server=self.get_legacy_server()
#			server.xend.domain.migrate(vmname, dest_node.get_hostname(), True, 0, -1, None)
#		else:
		try:
			vm=self.server.xenapi.VM.get_by_name_label(vmname)[0]
		except IndexError:
			raise NotRunningVmError(self.get_hostname(),vmname)
		self.server.xenapi.VM.migrate(vm,dest_node.get_hostname(),True,{'port':0,'node':-1,'ssl':None})

	def enable_vm_autostart(self, vmname):
		"""Create the autostart link for the specified vm."""
		self.run("test -L /etc/xen/auto/%s || ln -s /etc/xen/vm/%s /etc/xen/auto/" % (vmname, vmname))
		
	def disable_vm_autostart(self, vmname):
		"""Delete the autostart link for the specified vm."""
		self.run("rm -f /etc/xen/auto/"+vmname)
		
	def shutdown(self, vmname, hard=False):
		"""Shutdown the specified vm.

		If 'hard' is True, do a hard shutdown (destroy).
		Raise a NotRunningVmError if the vm is not running.
		"""
		try:
			vm=self.server.xenapi.VM.get_by_name_label(vmname)[0]
		except IndexError:
			raise NotRunningVmError(self.get_hostname(),vmname)

		if hard:
			self.server.xenapi.VM.hard_shutdown(vm)
		else:
			self.server.xenapi.VM.clean_shutdown(vm)

		# Wait until VM is down
		time.sleep(1)
		timout=0
		while(self.is_vm_started(vmname) and timout<=core.cfg['SHUTDOWN_TIMEOUT']):
			if not core.cfg['QUIET']: 
				sys.stdout.write(".")
				sys.stdout.flush()
			time.sleep(1)
			timout += 1

		# If vm is still running, destroy it
		if self.is_vm_started(vmname):
			if not core.cfg['QUIET']:
				print " Timeout !\nNo response from VM, destroying it ..."
			self.server.xenapi.VM.hard_shutdown(vm)
			time.sleep(1)

		self.deactivate_lv(vmname)
	
	def shutdown_all(self, hard=False):
		"""Shutdown all running vm on this node.

		If 'hard' is True, do a hard shutdown (destroy).
		"""

		for vmname in self.get_vms_names():
			try:
				self.shutdown(vmname, hard)
			except NotRunningVmError:
				continue

	def get_vm(self, vmname):
		"""Return the VM instance corresponding to the given vmname."""
		try:
			uuid=self.server.xenapi.VM.get_by_name_label(vmname)[0]
		except IndexError:
			raise NotRunningVmError(self.get_hostname(),vmname)
		vm_rec=self.server.xenapi.VM.get_record(uuid)
		vm=VM(vm_rec['name_label'],vm_rec['domid'])
		vm.metrics=self.server.xenapi.VM_metrics.get_record(vm_rec['metrics'])
		return vm

	def get_vms(self, nocache=False):
		"""
		Return the list of VM instance for each running vm.
		Result will be cached for 5 seconds, unless 'nocache' is True.
		"""

		def _get_vms():
			vms=list()
			dom_recs = self.server.xenapi.VM.get_all_records()
			dom_metrics_recs = self.server.xenapi.VM_metrics.get_all_records()
			log.debug("[API]", self.hostname, "dom_recs=", dom_recs)
			log.debug("[API]", self.hostname, "dom_metrics_recs=", dom_metrics_recs)

			for dom_rec in dom_recs.values():
				if dom_rec['name_label'] == "Domain-0":
					continue # Discard Dom0
				if dom_rec['name_label'].startswith("migrating-"):
					continue # Discard migration temporary vm
				if dom_rec['power_state'] == "Halted":
					# power_state could be: Halted, Paused, Running, Suspended, Crashed, Unknown
					continue # Discard non instantiated vm

				vm=VM(dom_rec['name_label'],dom_rec['domid'])
				vm.metrics=dom_metrics_recs[dom_rec['metrics']]
				vms.append(vm)

			return vms

		return self._cache.cache(5, nocache, _get_vms)

	def get_vms_names(self, nocache=False):
		"""
		Return the list of running vm.
		Result will be cached for 5 seconds, unless 'nocache' is True.
		"""

		def _get_vms_names():
			vms_names=list()
			dom_recs = self.server.xenapi.VM.get_all_records()
			log.debug("[API]", self.hostname, "dom_recs=", dom_recs)

			for dom_rec in dom_recs.values():
				if dom_rec['name_label'] == "Domain-0":
					continue # Discard Dom0
				if dom_rec['name_label'].startswith("migrating-"):
					continue # Discard migration temporary vm
				if dom_rec['power_state'] == "Halted":
					# power_state could be: Halted, Paused, Running, Suspended, Crashed, Unknown
					continue # Discard non instantiated vm

				vms_names.append(dom_rec['name_label'])

			return vms_names

		return self._cache.cache(5, nocache, _get_vms_names)

	def get_possible_vm_names(self, name=""): 
		"""
		Return the list of possible vm name, based on file's names in the configuration directory.
		You can use globbing (bash syntax) to match names.
		If there is no match, return an empty list.
		"""

		names=list()
		for file in self.run("ls %s/%s* || true" % (core.cfg['VMCONF_DIR'], name)).readlines():
			name=os.path.basename(file.strip()).rsplit(".cfg",1)[0]
			names.append(name)

		return names

	def check_missing_lvs(self):
		"""
		Perform a check on logicals volumes used by VMs. 
		Return False if some are missing.
		"""
		log.info("Checking for missing LV...")
		safe=True
		
		# Get all LVs used by VMs
		used_lvs = list()
		for vm in self.get_possible_vm_names():
			used_lvs.extend(VM(vm).get_lvs())

		# Get all existent LVs
		existent_lvs = list()
		for line in self.run("lvs -o vg_name,name --noheading").readlines():
			(vg, lv)=line.strip().split()
			existent_lvs.append("/dev/"+vg+"/"+lv)
		
		# Compute missing LVs 
		missing_lvs = list(Set(used_lvs) - Set(existent_lvs))
		if len(missing_lvs):
			log.info(" ** WARNING : Found missing LV :\n\t", "\n\t".join(missing_lvs))
			safe=False

		return safe

	def check_activated_lvs(self):
		"""
		Perform a sanity check of the LVM activation on this node.
		Return False if there is some inconsistencies.
		"""
		log.info("Checking LV activation on", self.get_hostname(), "...")
		safe=True

		# Get all active LVs on the node
		regex = re.compile('.{4}a.')
		active_lvs = list()
		for line in self.run("lvs -o vg_name,name,attr --noheading").readlines():
			(vg, lv, attr)=line.strip().split()
			if regex.search(attr) != None:
				active_lvs.append("/dev/"+vg+"/"+lv)

		# Get all LVs used by VMs
		used_lvs = list()
		for vm in self.get_possible_vm_names():
			used_lvs.extend(VM(vm).get_lvs())

		# Compute the intersection of the two lists (active and used LVs)
		active_and_used_lvs = list(Set(active_lvs) & Set(used_lvs))
		log.debug("[NODE]", self.hostname, "active_and_used_lvs=", active_and_used_lvs)

		# Get all LVs of running VM
		running_lvs = [ lv for vm in self.get_vms() for lv in vm.get_lvs() ]
		log.debug("[NODE]", self.hostname, "running_lvs=", running_lvs)

		# Compute activated LVs without running vm
		lvs_without_vm = list(Set(active_and_used_lvs) - Set(running_lvs))
		if len(lvs_without_vm):
			log.info(" ** WARNING : Found activated LV without running VM :\n\t", "\n\t".join(lvs_without_vm))
			safe=False

		# Compute running vm without activated LVs 
		vm_without_lvs = list(Set(running_lvs) - Set(active_and_used_lvs))
		if len(vm_without_lvs):
			log.info(" ** WARNING : Found running VM without activated LV :\n\t", "\n\t".join(vm_without_lvs))
			safe=False

		return safe

	def check_autostart(self):
		"""Perform a sanity check of the autostart links."""
		log.info("Checking autostart links on", self.get_hostname(), "...")
		safe=True

		# Get all autostart links on the node
		links = [ link.strip() for link in self.run("ls /etc/xen/auto/").readlines() ]
		log.debug("[NODE]", self.hostname, "links=", links)

		# Get all running VM
		running_vms = [ vm.name for vm in self.get_vms() ]
		log.debug("[NODE]", self.hostname, "running_vms=", running_vms)

		# Compute running vm without autostart link
		link_without_vm = list(Set(links) - Set(running_vms))
		if len(link_without_vm):
			log.info(" ** WARNING : Found autostart link without running VM :\n\t", "\n\t".join(link_without_vm))
			safe=False

		# Compute running vm without autostart link
		vm_without_link = list(Set(running_vms) - Set(links))
		if len(vm_without_link):
			log.info(" ** WARNING : Found running VM without autostart link :\n\t", "\n\t".join(vm_without_link))
			safe=False

		return safe

	def ping(self, hostnames):
		"""Return True if one or more hostname is alive"""

		if not isinstance(hostnames, list):
			hostnames=[hostnames]

		if len(hostnames)==0:
			return False

		# TODO: handle DNS failure and missing command ?
		return "alive" in self.run("fping -r1 " + " ".join(hostnames) + "|| true").read()

	def fence(self, hostname):
		"""
		Fence the given node. 

		You have to make a fencing script that will use iLo, IPMI or other such fencing device.
		See FENCE_CMD in configuration file.

		Raise a FenceNodeError if the fence fail of if DISABLE_FENCING is True.
		"""
		if core.cfg['DISABLE_FENCING']:
			raise FenceNodeError(self.get_hostname(), "Fencing disabled by configuration", hostname)

		if self.get_hostname() == hostname:
			log.warn("Node is self-fencing !")
			log.warn("\"Chérie ça va trancher.\"")

		try:
			self.run(core.cfg['FENCE_CMD'] + " " + hostname)
		except ShellError, e:
			raise FenceNodeError(self.get_hostname(), e.value, hostname)

	# Define accessors 
	legacy_server = property(get_legacy_server)
	metrics = property(get_metrics)


class ClusterNodeError(Exception):
	"""This class is the main class for all errors relatives to the node."""

	def __init__(self, nodename, value=""):
		self.nodename=nodename
		self.value=value

	def __str__(self):
		return "Error on %s : %s" % (self.nodename, self.value)

class ShellError(ClusterNodeError):
	"""This class is used to raise error when local exec fail."""

	def __init__(self, nodename, value, exitcode):
		# Why don't use super() ? because python really sucks !
		ClusterNodeError.__init__(self, nodename, value)
		self.exitcode=exitcode

class SSHError(ShellError):
	"""This class is used to raise error when SSH exec fail."""
	pass

class RunningVmError(ClusterNodeError):
	"""This class is used when a VM is running and should'nt."""

	def __str__(self):
		return "Error on %s : VM %s is running." % (self.nodename, self.value)

class NotRunningVmError(ClusterNodeError):
	"""This class is used when a VM is not running and should be."""

	def __str__(self):
		return "Error on %s : VM %s is not running here." % (self.nodename, self.value)

class NotEnoughRamError(ClusterNodeError):
	"""This class is used when there is not enough ram."""

	def __str__(self):
		return "Error on %s : There is not enough ram: %s" % (self.nodename, self.value)

class FenceNodeError(ClusterNodeError):
	"""This class is used to raise error when fencing fail."""

	def __init__(self, nodename, value, hostname=""):
		# Why don't use super() ? because python really sucks !
		ClusterNodeError.__init__(self, nodename, value)
		self.hostname=hostname

	def __str__(self):
		return "Error on %s : Cannot fence %s : %s" % (self.nodename, self.hostname, self.value)

class XenError(ClusterNodeError):
	"""This class is used when an internal error from Xen is detected."""

	def __str__(self):
		return "XEN Error on %s : %s" % (self.nodename, self.value)


# vim: ts=4:sw=4:ai
