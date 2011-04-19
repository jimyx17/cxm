#!/usr/bin/python
# -*- coding:Utf-8 -*-

# cxm - Clustered Xen Management API and tools
# Copyleft 2011 - Nicolas AGIUS <nagius@astek.fr>
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

from twisted.application import service
import signal
from cxm.master import MasterService
import cxm.logs, cxm.core

# Define the name of this daemon
name="cxmd"
cxm.core.cfg['QUIET']=True

# Get the service
master=MasterService()
application = service.Application(name)
master.setServiceParent(application)

# Start logger subsystem
cxm.logs.init(name)

# Reinstall signal ignored by xen
signal.signal(signal.SIGINT, lambda signum,frame: master.stopService())

# vim: ts=4:sw=4:ai
