# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# (C) British Crown Copyright 2012-8 Met Office.
#
# This file is part of Rose, a framework for meteorological suites.
#
# Rose is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Rose is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Rose. If not, see <http://www.gnu.org/licenses/>.
# -----------------------------------------------------------------------------
"""A handler of locations on remote hosts."""

from tempfile import TemporaryFile
from time import sleep, time
from rose.popen import RosePopenError


class RsyncLocHandler(object):
    """Handler of locations on remote hosts."""

    SCHEME = "rsync"
    TIMEOUT = 8

    def __init__(self, manager):
        self.manager = manager
        self.rsync = self.manager.popen.which("rsync")

    def can_pull(self, loc):
        """Return true if loc.name looks like a path on a remote host."""
        if self.rsync is None or ":" not in loc.name:
            return False
        host, path = loc.name.split(":", 1)
        if path.startswith("//") or host == "fcm":
            # loc.name is a URL or FCM location keyword, not a host:path
            return False
        cmd = self.manager.popen.get_cmd("ssh", "-n", host, "test", "-e", path)
        try:
            proc = self.manager.popen.run_bg(*cmd)
            end_time = time() + self.TIMEOUT
            while proc.poll() is None and time < end_time:
                sleep(0.1)
            if proc.poll():
                proc.kill()
        except RosePopenError:
            return False
        else:
            return proc.wait() == 0

    def parse(self, loc, _):
        """Set loc.scheme, loc.loc_type, loc.paths."""
        loc.scheme = "rsync"
        # Attempt to obtain the checksum(s) via "ssh"
        host, path = loc.name.split(":", 1)
        cmd = self.manager.popen.get_cmd(
            "ssh", host, "python", "-", path, loc.TYPE_BLOB, loc.TYPE_TREE)
        temp_file = TemporaryFile()
        temp_file.write(r"""
import os
import sys
path, str_blob, str_tree = sys.argv[1:]
if os.path.isdir(path):
    print str_tree
    os.chdir(path)
    for dirpath, dirnames, filenames in os.walk(path):
        good_dirnames = []
        for dirname in dirnames:
            if not dirname.startswith("."):
                good_dirnames.append(dirname)
                name = os.path.join(dirpath, dirname)
                print "-", "-", "-", name
        dirnames[:] = good_dirnames
        for filename in filenames:
            if filename.startswith("."):
                continue
            name = os.path.join(dirpath, filename)
            stat = os.stat(name)
            print oct(stat.st_mode), stat.st_mtime, stat.st_size, name
elif os.path.isfile(path):
    print str_blob
    stat = os.stat(path)
    print oct(stat.st_mode), stat.st_mtime, stat.st_size, path
""")
        temp_file.seek(0)
        out = self.manager.popen(*cmd, stdin=temp_file)[0]
        lines = out.splitlines()
        if not lines or lines[0] not in [loc.TYPE_BLOB, loc.TYPE_TREE]:
            raise ValueError(loc.name)
        loc.loc_type = lines.pop(0)
        if loc.loc_type == loc.TYPE_BLOB:
            line = lines.pop(0)
            access_mode, mtime, size, name = line.split(None, 3)
            fake_sum = "source=%s:mtime=%s:size=%s" % (
                name, mtime, size)
            loc.add_path(loc.BLOB, fake_sum, int(access_mode))
        else:  # if loc.loc_type == loc.TYPE_TREE:
            for line in lines:
                access_mode, mtime, size, name = line.split(None, 3)
                if mtime == "-" or size == "-":
                    fake_sum = None
                else:
                    access_mode = int(access_mode)
                    fake_sum = "source=%s:mtime=%s:size=%s" % (
                        name, mtime, size)
                loc.add_path(name, fake_sum, access_mode)

    def pull(self, loc, _):
        """Run "rsync" to pull files or directories of loc to its cache."""
        name = loc.name
        if loc.loc_type == loc.TYPE_TREE:
            name = loc.name + "/"
        cmd = self.manager.popen.get_cmd("rsync", name, loc.cache)
        self.manager.popen(*cmd)
