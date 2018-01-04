#!/usr/bin/env python3
import argparse
import datetime
import json
import multiprocessing
import os
import signal
import sys
import time

from . import dbus_listener
from . import strip

class RGBd:
	def __init__(self, confpath):
		self.conf = self.loadconf(confpath)
		self.blank_on_exit = self.conf.get("blank_on_exit", False)

		self.do_daemonize = self.conf.get("daemonize", True)
		if (self.do_daemonize == False):
			return
		d_conf = self.conf.get("daemon", {})
		# we never use STDIN ever again anyways... but this is tradition.
		self.daemon_stdin   = d_conf.get("stdin",  os.devnull)
		self.daemon_in_md   = d_conf.get("stdin_mode", "r")

		self.daemon_stdout  = d_conf.get("stdout", os.devnull)
		self.daemon_out_md  = d_conf.get("stdout_mode", "a")

		self.daemon_stderr  = d_conf.get("stderr", os.devnull)
		self.daemon_err_md  = d_conf.get("stderr_mode", "a")

		self.daemon_chdir   = d_conf.get("working_dir", "/")
		self.daemon_pidfile = d_conf.get("pidfile", "/tmp/rgbd.pid")

	def loadconf(self, path):
		with open(path) as conf:
			return json.load(conf)

	def run(self):
		if (sys.version_info.major < 3):
			raise Exception("must be running at least python 3")
		if (os.getuid() == 0 or os.geteuid() == 0 or os.getgid() == 0):
			raise Exception("This daemon should not be run as root due to security reasons.\nPlease control your ws281x using SPI instead of PWM.")

		self.queue = multiprocessing.Queue()
		self.listener_thread = multiprocessing.Process(target=dbus_listener.Listener, args=(self.queue,), daemon=True)
		self.listener_thread.start()

		self.strip = strip.Strip(self.conf, self)
		self.strip.animate(self.queue)

	def reload(self):
		# ideally this should reload configs in-place
		self.restart()

	def cleanup(self, signum=None, frame=None):
		if (self.blank_on_exit):
			self.strip.blank_strip()
		os.kill(self.listener_thread.pid, signal.SIGINT)
		self.listener_thread.join(timeout=0.25)

		if (self.listener_thread.is_alive()):
			os.kill(self.listener_thread.pid, signal.SIGKILL)
			print("Had to SIGKILL child - pretty bad; potential orphans")

		# done with cleanup - we can let other processes proceed now
		try:
			os.remove(self.daemon_pidfile)
		except FileNotFoundError as e:
			pass

		print("rgbd exited successfully at {}".format(datetime.datetime.now()))
		sys.stdin.close()
		sys.stdout.close()
		sys.stderr.close()
		sys.exit(0)

	"""
		daemonization code adapted from
		http://web.archive.org/web/20131017130434/http://www.jejik.com/articles/2007/02/a_simple_unix_linux_daemon_in_python/
	"""

	def daemonize(self):
		# useful for debugging animations, for now.
		if (not self.do_daemonize):
			return
		# first of the double fork
		try:
			pid = os.fork()
			if (pid > 0):
				sys.exit(0)
		except OSError as e:
			sys.stderr.write("first fork failed: {}\n".format(str(e)))
			sys.exit(1)

		os.chdir(self.daemon_chdir)
		os.setsid()
		os.umask(0)
		# second fork magic
		try:
			pid = os.fork()
			if (pid > 0):
				sys.exit(0)
		except OSError as e:
			sys.stderr.write("second fork failed: {}\n".format(str(e)))
			sys.exit(1)

		# redirect stdout/stderr
		sys.stdout.flush()
		sys.stderr.flush()
		stdin  = open(self.daemon_stdin,  self.daemon_in_md )
		stdout = open(self.daemon_stdout, self.daemon_out_md)
		stderr = open(self.daemon_stderr, self.daemon_err_md)

		os.dup2(stdin.fileno(),  sys.stdin.fileno() )
		os.dup2(stdout.fileno(), sys.stdout.fileno())
		os.dup2(stderr.fileno(), sys.stderr.fileno())

		# bind signals
		signal.signal(signal.SIGINT,  self.cleanup)
		signal.signal(signal.SIGTERM, self.cleanup)
		signal.signal(signal.SIGHUP,  self.reload )

		# create pidfile
		with open(self.daemon_pidfile, "w") as pidf:
			pidf.write("{}\n".format(os.getpid()))

		# TODO: set up good logging / verbosity levels
		print("Daemonization finished at {}".format(datetime.datetime.now()))

	def start(self):
		""" check for pidfile, if not exists, daemonize(), run() """
		try:
			with open(self.daemon_pidfile, "r") as pidf:
				pid = int(pidf.read().strip())
		except FileNotFoundError as e:
			pid = None

		if pid:
			sys.stderr.write("pidfile exists with value {}. There is either a daemon already running, or a daemon crashed and didn't clean up.\n".format(pid))
			sys.exit(1)

		self.daemonize()
		self.run()
		pass

	def stop(self, restart=False):
		""" checks for pidfile; if exists, read the pid, send SIGTERM """
		try:
			with open(self.daemon_pidfile, "r") as pidf:
				pid = int(pidf.read().strip())
		except FileNotFoundError as e:
			pid = None

		if (not pid):
			if (os.path.exists(self.daemon_pidfile)):
				os.remove(self.daemon_pidfile)
			if (restart):
				return
			sys.stderr.write("No pid found when stopping - daemon not running?\n")
			return
		# try to kill existing process / wipe out lockfile
		try:
			os.kill(pid, signal.SIGTERM)
			time.sleep(0.3)
			isalive = (os.kill(pid, 0) == None)
		except ProcessLookupError as e:
			isalive = False
		# maybe not catch this error and let it just raise()?
		except PermissionError as e:
			sys.stderr.write("Unable to kill existing process with pid {}.\n".format(pid))
			sys.exit(1)

		if (isalive):
			sys.stderr.write("Daemon didn't exit in time - possibly hung?\n")
			sys.exit(1)
		else:
			if (os.path.exists(self.daemon_pidfile)):
				os.remove(self.daemon_pidfile)
		return

	def restart(self):
		""" ez """
		self.stop(True)
		self.start()
