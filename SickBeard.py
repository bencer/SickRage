#!/usr/bin/env python2
# -*- coding: utf-8 -*-
# Author: echel0n <sickrage.tv@gmail.com>
# URL: http://www.github.com/sickragetv/sickrage/
#
# This file is part of SickRage.
#
# SickRage is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SickRage is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SickRage.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, unicode_literals

from time import strptime

strptime("2012", "%Y")

import os
import sys
import time
import signal
import getopt
import shutil
import logging
import datetime
import threading
import traceback
import subprocess

sys.path.insert(1, os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

# https://mail.python.org/pipermail/python-dev/2014-September/136300.html
if sys.version_info >= (2, 7, 9):
    import ssl

    ssl._create_default_https_context = ssl._create_unverified_context

import sickbeard
from sickbeard import db, network_timezones, failed_history, name_cache
from sickbeard.tv import TVShow
from sickbeard.webserveInit import SRWebServer
from sickbeard.event_queue import Events
from sickbeard.helpers import removetree
from sickrage.helper.encoding import ek, encodingInit
from configobj import ConfigObj


class SickRage(object):
    def __init__(self):
        # signal and event handlers
        signal.signal(signal.SIGINT, sickbeard.sig_handler)
        signal.signal(signal.SIGTERM, sickbeard.sig_handler)
        sickbeard.events = Events(self.shutdown)

        # daemon constants
        self.runAsDaemon = False
        self.CREATEPID = False
        self.PIDFILE = ''

        # webserver constants
        self.forcedPort = None
        self.noLaunch = False

        self.webhost = '0.0.0.0'
        self.startPort = sickbeard.WEB_PORT
        self.web_options = {}

        self.log_dir = None
        self.consoleLogging = True

    @staticmethod
    def help_message():
        """
        print help message for commandline options
        """
        help_msg = "\n"
        help_msg += "Usage: " + sickbeard.MY_FULLNAME + " <option> <another option>\n"
        help_msg += "\n"
        help_msg += "Options:\n"
        help_msg += "\n"
        help_msg += "    -h          --help              Prints this message\n"
        help_msg += "    -q          --quiet             Disables logging to console\n"
        help_msg += "                --nolaunch          Suppress launching web browser on startup\n"

        if sys.platform == 'win32' or sys.platform == 'darwin':
            help_msg += "    -d          --daemon            Running as real daemon is not supported on Windows\n"
            help_msg += "                                    On Windows and MAC, --daemon is substituted with: --quiet --nolaunch\n"
        else:
            help_msg += "    -d          --daemon            Run as double forked daemon (includes options --quiet --nolaunch)\n"
            help_msg += "                --pidfile=<path>    Combined with --daemon creates a pidfile (full path including filename)\n"

        help_msg += "    -p <port>   --port=<port>       Override default/configured port to listen on\n"
        help_msg += "                --datadir=<path>    Override folder (full path) as location for\n"
        help_msg += "                                    storing database, configfile, cache, logfiles \n"
        help_msg += "                                    Default: " + sickbeard.PROG_DIR + "\n"
        help_msg += "                --config=<path>     Override config filename (full path including filename)\n"
        help_msg += "                                    to load configuration from \n"
        help_msg += "                                    Default: config.ini in " + sickbeard.PROG_DIR + " or --datadir location\n"
        help_msg += "                --noresize          Prevent resizing of the banner/posters even if PIL is installed\n"

        return help_msg

    def start(self):
        # Rename the main thread
        threading.currentThread().name = "MAIN"

        # initalize encoding defaults
        encodingInit()

        # do some preliminary stuff
        sickbeard.MY_FULLNAME = ek(os.path.normpath, ek(os.path.abspath, __file__))
        sickbeard.MY_NAME = ek(os.path.basename, sickbeard.MY_FULLNAME)
        sickbeard.PROG_DIR = ek(os.path.dirname, sickbeard.MY_FULLNAME)
        sickbeard.DATA_DIR = sickbeard.PROG_DIR
        sickbeard.MY_ARGS = sys.argv[1:]

        # Need console logging for SickBeard.py and SickBeard-console.exe
        self.consoleLogging = (not hasattr(sys, "frozen")) or (sickbeard.MY_NAME.lower().find('-console') > 0)

        # Do this before importing sickbeard, to prevent locked files and incorrect import
        oldtornado = ek(os.path.abspath, ek(os.path.join, ek(os.path.dirname, __file__), 'tornado'))
        if ek(os.path.isdir, oldtornado):
            ek(shutil.move, oldtornado, oldtornado + '_kill')
            ek(removetree, oldtornado + '_kill')

        try:
            opts, _ = getopt.getopt(
                    sys.argv[1:], "hqdp::",
                    ['help', 'quiet', 'nolaunch', 'daemon', 'pidfile=', 'port=', 'datadir=', 'config=', 'noresize']
            )
        except getopt.GetoptError:
            sys.exit(self.help_message())

        for o, a in opts:
            # Prints help message
            if o in ('-h', '--help'):
                sys.exit(self.help_message())

            # For now we'll just silence the logging
            if o in ('-q', '--quiet'):
                self.consoleLogging = False

            # Suppress launching web browser
            # Needed for OSes without default browser assigned
            # Prevent duplicate browser window when restarting in the app
            if o in ('--nolaunch',):
                self.noLaunch = True

            # Override default/configured port
            if o in ('-p', '--port'):
                try:
                    self.forcedPort = int(a)
                except ValueError:
                    sys.exit("Port: " + str(a) + " is not a number. Exiting.")

            # Run as a double forked daemon
            if o in ('-d', '--daemon'):
                self.runAsDaemon = True
                # When running as daemon disable consoleLogging and don't start browser
                self.consoleLogging = False
                self.noLaunch = True

                if sys.platform == 'win32' or sys.platform == 'darwin':
                    self.runAsDaemon = False

            # Write a pidfile if requested
            if o in ('--pidfile',):
                self.CREATEPID = True
                self.PIDFILE = str(a)

                # If the pidfile already exists, sickbeard may still be running, so exit
                if ek(os.path.exists, self.PIDFILE):
                    sys.exit("PID file: " + self.PIDFILE + " already exists. Exiting.")

            # Specify folder to load the config file from
            if o in ('--config',):
                sickbeard.CONFIG_FILE = ek(os.path.abspath, a)

            # Specify folder to use as the data dir
            if o in ('--datadir',):
                sickbeard.DATA_DIR = ek(os.path.abspath, a)

            # Prevent resizing of the banner/posters even if PIL is installed
            if o in ('--noresize',):
                sickbeard.NO_RESIZE = True

        # The pidfile is only useful in daemon mode, make sure we can write the file properly
        if self.CREATEPID:
            if self.runAsDaemon:
                pid_dir = ek(os.path.dirname, self.PIDFILE)
                if not ek(os.access, pid_dir, os.F_OK):
                    sys.exit("PID dir: " + pid_dir + " doesn't exist. Exiting.")
                if not ek(os.access, pid_dir, os.W_OK):
                    sys.exit("PID dir: " + pid_dir + " must be writable (write permissions). Exiting.")

            else:
                if self.consoleLogging:
                    sys.stdout.write("Not running in daemon mode. PID file creation disabled.\n")

                self.CREATEPID = False

        # If they don't specify a config file then put it in the data dir
        if not sickbeard.CONFIG_FILE:
            sickbeard.CONFIG_FILE = ek(os.path.join, sickbeard.DATA_DIR, "config.ini")

        # Make sure that we can create the data dir
        result = ek(os.access, sickbeard.DATA_DIR, os.F_OK)
        if not ek(os.access, sickbeard.DATA_DIR, os.F_OK):
            try:
                ek(os.makedirs, sickbeard.DATA_DIR, 0o744)
            except os.error as e:
                raise SystemExit("Unable to create datadir '" + sickbeard.DATA_DIR + "'")

        # Make sure we can write to the data dir
        if not ek(os.access, sickbeard.DATA_DIR, os.W_OK):
            raise SystemExit("Datadir must be writeable '" + sickbeard.DATA_DIR + "'")

        # Make sure we can write to the config file
        if not ek(os.access, sickbeard.CONFIG_FILE, os.W_OK):
            if ek(os.path.isfile, sickbeard.CONFIG_FILE):
                raise SystemExit("Config file '" + sickbeard.CONFIG_FILE + "' must be writeable.")
            elif not ek(os.access, ek(os.path.dirname, sickbeard.CONFIG_FILE), os.W_OK):
                raise SystemExit(
                        "Config file root dir '" + ek(os.path.dirname, sickbeard.CONFIG_FILE) + "' must be writeable.")

        ek(os.chdir, sickbeard.DATA_DIR)

        # Check if we need to perform a restore first
        restoreDir = ek(os.path.join, sickbeard.DATA_DIR, 'restore')
        if ek(os.path.exists, restoreDir):
            success = self.restoreDB(restoreDir, sickbeard.DATA_DIR)
            if self.consoleLogging:
                sys.stdout.write("Restore: restoring DB and config.ini %s!\n" % ("FAILED", "SUCCESSFUL")[success])

        # Load the config and publish it to the sickbeard package
        if self.consoleLogging and not ek(os.path.isfile, sickbeard.CONFIG_FILE):
            sys.stdout.write("Unable to find '" + sickbeard.CONFIG_FILE + "' , all settings will be default!" + "\n")

        sickbeard.CFG = ConfigObj(sickbeard.CONFIG_FILE)

        # Initialize the config and our threads
        sickbeard.initialize(consoleLogging=self.consoleLogging)

        if self.runAsDaemon:
            sickbeard.DAEMONIZE = True
            self.daemonize()

        # Get PID
        sickbeard.PID = os.getpid()

        # Build from the DB to start with
        self.loadShowsFromDB()

        if self.forcedPort:
            logging.info("Forcing web server to port " + str(self.forcedPort))
            self.startPort = self.forcedPort
        else:
            self.startPort = sickbeard.WEB_PORT

        if sickbeard.WEB_LOG:
            self.log_dir = sickbeard.LOG_DIR
        else:
            self.log_dir = None

        # sickbeard.WEB_HOST is available as a configuration value in various
        # places but is not configurable. It is supported here for historic reasons.
        if sickbeard.WEB_HOST and sickbeard.WEB_HOST != '0.0.0.0':
            self.webhost = sickbeard.WEB_HOST
        else:
            if sickbeard.WEB_IPV6:
                self.webhost = '::'
            else:
                self.webhost = '0.0.0.0'

        # Clean up after update
        if sickbeard.GIT_NEWVER:
            toclean = ek(os.path.join, sickbeard.CACHE_DIR, 'mako')
            for root, dirs, files in ek(os.walk, toclean, topdown=False):
                for name in files:
                    ek(os.remove, ek(os.path.join, root, name))
                for name in dirs:
                    ek(os.rmdir, ek(os.path.join, root, name))
            sickbeard.GIT_NEWVER = False

        # Fire up all our threads
        logging.info("Starting SiCKRAGE:[{}] CONFIG:[{}]".format(sickbeard.BRANCH, sickbeard.CONFIG_FILE))
        sickbeard.start()

        # sure, why not?
        if sickbeard.USE_FAILED_DOWNLOADS:
            failed_history.trimHistory()

        # Check for metadata indexer updates for shows (Disabled until we use api)
        # sickbeard.showUpdateScheduler.forceRun()

        # start tornado web server
        sickbeard.WEB_SERVER = SRWebServer({
            'port': int(self.startPort),
            'host': self.webhost,
            'gui_root': sickbeard.GUI_DIR,
            'web_root': sickbeard.WEB_ROOT,
            'log_dir': self.log_dir,
            'username': sickbeard.WEB_USERNAME,
            'password': sickbeard.WEB_PASSWORD,
            'enable_https': sickbeard.ENABLE_HTTPS,
            'handle_reverse_proxy': sickbeard.HANDLE_REVERSE_PROXY,
            'https_cert': ek(os.path.join, sickbeard.PROG_DIR, sickbeard.HTTPS_CERT),
            'https_key': ek(os.path.join, sickbeard.PROG_DIR, sickbeard.HTTPS_KEY),
        }).start()

    def daemonize(self):
        """
        Fork off as a daemon
        """
        # pylint: disable=E1101,W0212
        # An object is accessed for a non-existent member.
        # Access to a protected member of a client class
        # Make a non-session-leader child process
        try:
            pid = os.fork()  # @UndefinedVariable - only available in UNIX
            if pid != 0:
                os._exit(0)
        except OSError as e:
            sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(1)

        os.setsid()  # @UndefinedVariable - only available in UNIX

        # https://github.com/SiCKRAGETV/sickrage-issues/issues/2969
        # http://www.microhowto.info/howto/cause_a_process_to_become_a_daemon_in_c.html#idp23920
        # https://www.safaribooksonline.com/library/view/python-cookbook/0596001673/ch06s08.html
        # Previous code simply set the umask to whatever it was because it was ANDing instead of ORring
        # Daemons traditionally run with umask 0 anyways and this should not have repercussions
        os.umask(0)

        # Make the child a session-leader by detaching from the terminal
        try:
            pid = os.fork()  # @UndefinedVariable - only available in UNIX
            if pid != 0:
                os._exit(0)
        except OSError as e:
            sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(1)

        # Write pid
        if self.CREATEPID:
            pid = str(os.getpid())
            logging.info("Writing PID: " + pid + " to " + str(self.PIDFILE))

            try:
                file(self.PIDFILE, 'w').write("%s\n" % pid)
            except IOError as e:
                logging.log_error_and_exit(
                        "Unable to write PID file: " + self.PIDFILE + " Error: " + str(e.strerror) + " [" + str(
                                e.errno) + "]")

        # Redirect all output
        sys.stdout.flush()
        sys.stderr.flush()

        devnull = getattr(os, 'devnull', '/dev/null')
        stdin = file(devnull, 'r')
        stdout = file(devnull, 'a+')
        stderr = file(devnull, 'a+')

        os.dup2(stdin.fileno(), getattr(sys.stdin, 'device', sys.stdin).fileno())
        os.dup2(stdout.fileno(), getattr(sys.stdout, 'device', sys.stdout).fileno())
        os.dup2(stderr.fileno(), getattr(sys.stderr, 'device', sys.stderr).fileno())

    @staticmethod
    def remove_pid_file(PIDFILE):
        try:
            if ek(os.path.exists, PIDFILE):
                ek(os.remove, PIDFILE)
        except (IOError, OSError):
            return False

        return True

    @staticmethod
    def loadShowsFromDB():
        """
        Populates the showList with shows from the database
        """
        logging.debug("Loading initial show list")

        myDB = db.DBConnection()
        sqlResults = myDB.select("SELECT * FROM tv_shows")

        sickbeard.showList = []
        for sqlShow in sqlResults:
            try:
                curShow = TVShow(int(sqlShow[b"indexer"]), int(sqlShow[b"indexer_id"]))

                # Build internal name cache for show
                name_cache.buildNameCache(curShow)

                # get next episode info
                curShow.nextEpisode()

                # add show to internal show list
                sickbeard.showList.append(curShow)
            except Exception as e:
                logging.error(
                        "There was an error creating the show in " + sqlShow[b"location"] + ": " + str(e).decode(
                                'utf-8'))
                logging.debug(traceback.format_exc())

    @staticmethod
    def restoreDB(srcDir, dstDir):
        try:
            filesList = ['sickbeard.db', 'config.ini', 'failed.db', 'cache.db']

            for filename in filesList:
                srcFile = ek(os.path.join, srcDir, filename)
                dstFile = ek(os.path.join, dstDir, filename)
                bakFile = ek(os.path.join, dstDir, '{0}.bak-{1}'.format(filename,
                                                                        datetime.datetime.now().strftime(
                                                                            '%Y%m%d_%H%M%S')))
                if ek(os.path.isfile, dstFile):
                    ek(shutil.move, dstFile, bakFile)
                ek(shutil.move, srcFile, dstFile)
            return True
        except Exception:
            return False

    def shutdown(self, event):
        if sickbeard.started:
            # stop all tasks
            sickbeard.halt()

            # save all shows to DB
            sickbeard.saveAll()

            # shutdown web server
            if sickbeard.WEB_SERVER:
                logging.info("Shutting down Tornado")
                sickbeard.WEB_SERVER.shutDown()

                try:
                    sickbeard.WEB_SERVER.join(10)
                except Exception:
                    pass

            # if run as daemon delete the pidfile
            if self.runAsDaemon and self.CREATEPID:
                self.remove_pid_file(self.PIDFILE)

            if event == sickbeard.event_queue.Events.SystemEvent.RESTART:
                install_type = sickbeard.versionCheckScheduler.action.install_type

                popen_list = []

                if install_type in ('git', 'source'):
                    popen_list = [sys.executable, sickbeard.MY_FULLNAME]
                elif install_type == 'win':
                    logging.error("You are using a binary Windows build of SiCKRAGE. Please switch to using git.")

                if popen_list and not sickbeard.NO_RESTART:
                    popen_list += sickbeard.MY_ARGS
                    if '--nolaunch' not in popen_list:
                        popen_list += ['--nolaunch']
                    logging.info("Restarting SiCKRAGE with " + str(popen_list))
                    logging.shutdown()  # shutdown the logger to make sure it's released the logfile BEFORE it restarts SR.
                    subprocess.Popen(popen_list, cwd=os.getcwd())

        # system exit
        logging.shutdown()  # Make sure the logger has stopped, just in case
        # pylint: disable=W0212
        # Access to a protected member of a client class
        os._exit(0)

if __name__ == "__main__":
    if sys.version_info < (2, 7):
        print("Sorry, SiCKRAGE requires Python 2.7+")
        sys.exit(1)

    # start sickrage
    SickRage().start()
