#!/usr/bin/env python
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
"""A pre-commit hook on a Rosie Subversion repository.

Ensure that commits conform to the rules of Rosie.

"""


from fnmatch import fnmatch
import os
import re
import rose
from rose.config import ConfigLoader
from rose.opt_parse import RoseOptionParser
from rose.macro import add_meta_paths, get_reports_as_text, load_meta_config
from rose.macros import DefaultValidators
from rose.popen import RosePopener
from rose.reporter import Reporter
from rose.resource import ResourceLocator
from rose.scheme_handler import SchemeHandlersManager
import shlex
import sys
import tempfile
import traceback


class BadChange(Exception):

    """An error representing a bad change in a commit transaction."""

    FMT_HEAD = "%s: %-4s%s"
    FMT_TAIL_1 = ": %s"
    FMT_TAIL_M = ":\n%s"
    NO_INFO = "SUITE MUST HAVE INFO FILE"
    NO_OWNER = "SUITE MUST HAVE OWNER"
    NO_TRUNK = "SUITE MUST HAVE TRUNK"
    PERM = "PERMISSION DENIED"
    USER = "NO SUCH USER"
    VALUE = "BAD VALUE IN FILE"

    def __init__(self, status, path, reason=PERM, content=""):
        Exception.__init__(self, status, path, reason, content)
        self.status = status
        self.path = path
        self.reason = reason
        self.content = content

    def __str__(self):
        tail = ""
        if self.content and "\n" in self.content:
            tail = self.FMT_TAIL_M % self.content
        elif self.content:
            tail = self.FMT_TAIL_1 % self.content
        return self.FMT_HEAD % (self.reason, self.status, self.path) + tail


class BadChanges(Exception):

    """An error representing bad changes in a commit transaction."""

    def __str__(self):
        bad_changes = self.args[0]
        return "\n".join([str(bad_change) for bad_change in bad_changes])


class RosieSvnPreCommitHook(object):

    """A pre-commit hook on a Rosie Subversion repository.

    Ensure that commits conform to the rules of Rosie.

    """

    IGNORES = "svnperms.conf"
    RE_ID_NAMES = [r"[Ra-z]", r"[Oa-z]", r"[S\d]", r"[I\d]", r"[E\d]"]
    LEN_ID = len(RE_ID_NAMES)
    ST_ADD = "A"
    ST_DELETE = "D"
    ST_UPDATE = "U"
    TRUNK_INFO_FILE = "trunk/rose-suite.info"
    TRUNK_KNOWN_KEYS_FILE = "trunk/rosie-keys"

    def __init__(self, event_handler=None, popen=None):
        if event_handler is None:
            event_handler = Reporter()
        self.event_handler = event_handler
        if popen is None:
            popen = RosePopener(self.event_handler)
        self.popen = popen
        path = os.path.dirname(os.path.dirname(sys.modules["rosie"].__file__))
        self.usertools_manager = SchemeHandlersManager(
            [path], "rosie.usertools", ["verify_users"])

    def _get_access_info(self, info_node):
        """Return (owner, access_list) from "info_node"."""
        owner = info_node.get_value(["owner"])
        access_list = info_node.get_value(["access-list"], "").split()
        access_list.sort()
        return owner, access_list

    def _get_info(self, repos, path_head, txn=None):
        """Return a ConfigNode for the "rose-suite.info" of a suite.

        The suite is located under path_head.

        """
        opt_txn = []
        if txn is not None:
            opt_txn = ["-t", txn]
        t_handle = tempfile.TemporaryFile()
        path = path_head + self.TRUNK_INFO_FILE
        t_handle.write(self._svnlook("cat", repos, path, *opt_txn))
        t_handle.seek(0)
        info_node = ConfigLoader()(t_handle)
        t_handle.close()
        return info_node

    def _svnlook(self, *args):
        """Return the standard output from "svnlook"."""
        command = ["svnlook"] + list(args)
        return self.popen(*command, stderr=sys.stderr)[0]

    def _verify_users(self, status, path, txn_owner, txn_access_list,
                      bad_changes):
        """Check txn_owner and txn_access_list.

        For any invalid users, append to bad_changes and return True.

        """
        # The owner and names in access list must be real users
        conf = ResourceLocator.default().get_conf()
        user_tool_name = conf.get_value(["rosa-svn", "user-tool"])
        if not user_tool_name:
            return False
        user_tool = self.usertools_manager.get_handler(user_tool_name)
        txn_users = set([txn_owner] + txn_access_list)
        txn_users.discard("*")
        bad_users = user_tool.verify_users(txn_users)
        for bad_user in bad_users:
            if txn_owner == bad_user:
                bad_change = BadChange(
                    status,
                    path,
                    BadChange.USER,
                    "owner=" + bad_user)
                bad_changes.append(bad_change)
            if bad_user in txn_access_list:
                bad_change = BadChange(
                    status,
                    path,
                    BadChange.USER,
                    "access-list=" + bad_user)
                bad_changes.append(bad_change)
        return bool(bad_users)

    def run(self, repos, txn):
        """Apply the rule engine on transaction "txn" to repository "repos"."""

        changes = set()  # set([(status, path), ...])
        for line in self._svnlook("changed", "-t", txn, repos).splitlines():
            status, path = line.split(None, 1)
            changes.add((status, path))
        bad_changes = []
        author = None
        super_users = None
        rev_info_map = {}  # {path-id: (owner, access-list), ...}
        txn_info_map = {}

        conf = ResourceLocator.default().get_conf()
        ignores_str = conf.get_value(["rosa-svn", "ignores"], self.IGNORES)
        ignores = shlex.split(ignores_str)

        for status, path in sorted(changes):
            if any(fnmatch(path, ignore) for ignore in ignores):
                continue

            names = path.split("/", self.LEN_ID + 1)
            tail = None
            if not names[-1]:
                tail = names.pop()

            # Directories above the suites must match the ID patterns
            is_bad = False
            for name, pattern in zip(names, self.RE_ID_NAMES):
                if not re.compile(r"\A" + pattern + r"\Z").match(name):
                    is_bad = True
                    break
            if is_bad:
                bad_changes.append(BadChange(status, path))
                continue

            # Can only add directories at levels above the suites
            if len(names) < self.LEN_ID:
                if status[0] != self.ST_ADD:
                    bad_changes.append(BadChange(status, path))
                continue
            else:
                is_meta_suite = "".join(names[0:self.LEN_ID]) == "ROSIE"

            # A file at the branch level
            if len(names) == self.LEN_ID + 1 and tail is None:
                bad_changes.append(BadChange(status, path))
                continue

            # No need to check non-trunk changes
            if len(names) > self.LEN_ID and names[self.LEN_ID] != "trunk":
                continue

            # New suite should have an info file
            if status[0] == self.ST_ADD and len(names) == self.LEN_ID:
                if (self.ST_ADD, path + "trunk/") not in changes:
                    bad_changes.append(
                        BadChange(status, path, BadChange.NO_TRUNK))
                    continue
                path_trunk_info_file = path + self.TRUNK_INFO_FILE
                if ((self.ST_ADD, path_trunk_info_file) not in changes and
                        (self.ST_UPDATE, path_trunk_info_file) not in changes):
                    bad_changes.append(
                        BadChange(status, path, BadChange.NO_INFO))
                continue

            # The rest are trunk changes in a suite
            path_head = "/".join(names[0:self.LEN_ID]) + "/"
            path_tail = path[len(path_head):]

            # For meta suite, make sure keys in keys file can be parsed
            if is_meta_suite and path_tail == self.TRUNK_KNOWN_KEYS_FILE:
                out = self._svnlook("cat", "-t", txn, repos, path)
                try:
                    shlex.split(out)
                except ValueError:
                    bad_changes.append(
                        BadChange(status, path, BadChange.VALUE))
                    continue

            # Suite trunk information file must have an owner
            # User IDs of owner and access list must be real
            if (status not in self.ST_DELETE and
                    path_tail == self.TRUNK_INFO_FILE):
                if path_head not in txn_info_map:
                    txn_info_map[path_head] = self._get_info(
                        repos, path_head, txn)
                txn_owner, txn_access_list = self._get_access_info(
                    txn_info_map[path_head])
                if not txn_owner:
                    bad_changes.append(
                        BadChange(status, path, BadChange.NO_OWNER))
                    continue
                if self._verify_users(
                        status, path, txn_owner, txn_access_list, bad_changes):
                    continue
                reports = DefaultValidators().validate(
                    txn_info_map[path_head],
                    load_meta_config(
                        txn_info_map[path_head],
                        config_type=rose.INFO_CONFIG_NAME))
                if reports:
                    reports_str = get_reports_as_text({None: reports}, path)
                    bad_changes.append(
                        BadChange(status, path, BadChange.VALUE, reports_str))
                    continue

            # Can only remove trunk information file with suite
            if status == self.ST_DELETE and path_tail == self.TRUNK_INFO_FILE:
                if (self.ST_DELETE, path_head) not in changes:
                    bad_changes.append(
                        BadChange(status, path, BadChange.NO_INFO))
                continue

            # Can only remove trunk with suite
            if status == self.ST_DELETE and path_tail == "trunk/":
                if (self.ST_DELETE, path_head) not in changes:
                    bad_changes.append(
                        BadChange(status, path, BadChange.NO_TRUNK))
                continue

            # New suite trunk: ignore the rest
            if (self.ST_ADD, path_head + "trunk/") in changes:
                continue

            # See whether author has permission to make changes
            if author is None:
                author = self._svnlook("author", "-t", txn, repos).strip()
            if super_users is None:
                super_users = []
                for s_key in ["rosa-svn", "rosa-svn-pre-commit"]:
                    value = conf.get_value([s_key, "super-users"])
                    if value is not None:
                        super_users = shlex.split(value)
                        break
            if path_head not in rev_info_map:
                rev_info_map[path_head] = self._get_info(
                    repos, path_head)
            owner, access_list = self._get_access_info(rev_info_map[path_head])
            admin_users = super_users + [owner]

            # Only admin users can remove the suite
            if author not in admin_users and not path_tail:
                bad_changes.append(BadChange(status, path))
                continue

            # Admin users and those in access list can modify everything in
            # trunk apart from specific entries in the trunk info file
            if "*" in access_list or author in admin_users + access_list:
                if path_tail != self.TRUNK_INFO_FILE:
                    continue
            else:
                bad_changes.append(BadChange(status, path))
                continue

            # The owner must not be deleted
            if path_head not in txn_info_map:
                txn_info_map[path_head] = self._get_info(
                    repos, path_head, txn)
            txn_owner, txn_access_list = self._get_access_info(
                txn_info_map[path_head])
            if not txn_owner:
                bad_changes.append(BadChange(status, path, BadChange.NO_OWNER))
                continue

            # Only the admin users can change owner and access list
            if owner == txn_owner and access_list == txn_access_list:
                continue
            if author not in admin_users:
                if owner != txn_owner:
                    bad_changes.append(BadChange(status, path, BadChange.PERM,
                                                 "owner=" + txn_owner))
                else:  # access list
                    bad_change = BadChange(
                        status, path, BadChange.PERM,
                        "access-list=" + " ".join(txn_access_list))
                    bad_changes.append(bad_change)
                continue

        if bad_changes:
            raise BadChanges(bad_changes)

    __call__ = run


def main():
    """Implement "rosa svn-pre-commit"."""
    add_meta_paths()
    opt_parser = RoseOptionParser()
    opts, args = opt_parser.parse_args()
    repos, txn = args
    report = Reporter(opts.verbosity - opts.quietness)
    hook = RosieSvnPreCommitHook(report)
    try:
        hook(repos, txn)
    except Exception as exc:
        report(exc)
        if opts.debug_mode:
            traceback.print_exc(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
