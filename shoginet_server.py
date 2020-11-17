import requests
import subprocess
import logging
import time
import urllib.parse as urlparse
import threading
import sys
import os
import platform


__version__ = "1.0.0"

DEFAULT_ENDPOINT = "http://localhost:9663/fishnet/"
KEY = ""

LVL_SKILL = [0, 3, 6, 10, 14, 16, 18, 20]
LVL_MOVETIMES = [50, 100, 150, 200, 300, 400, 500, 1000]
LVL_DEPTHS = [5, 5, 5, 5, 5, 8, 13, 22]
HTTP_TIMEOUT = 15.0

ENGINE = 5
logging.addLevelName(ENGINE, "ENGINE")

def create_header(info):
	return {
			"fishnet": {
				"version": __version__,
				"python": platform.python_version(),
				"apikey": KEY,
			},
			"stockfish": info,
		}

class Stockfish:
	def __init__(self, com, mem, thr):
		self.memory = mem
		self.threads = thr

		self.info = None
		self.process = None
		self.command = com

	def __open_stockfish(self):
		kwargs = {
			"shell": True,
			"stdout": subprocess.PIPE,
			"stderr": subprocess.STDOUT,
			"stdin": subprocess.PIPE,
			"bufsize": 1,  # Line buffered
			"universal_newlines": True,
		}
		# Prevent signal propagation from parent process
		try:
			kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
		except AttributeError:
			kwargs["preexec_fn"] = os.setpgrp
		return subprocess.Popen("./" + self.command, **kwargs)

	def __kill_stockfish(self):
		os.killpg(self.process.pid, signal.SIGKILL)

	# Inputs into engine
	def __send(self, line):
		logging.log(ENGINE, "%s << %s", self.process.pid, line)
		self.process.stdin.write(line + "\n")
		self.process.stdin.flush()

	# Reads output from the engine 
	def __recv(self):
		while True:
			line = self.process.stdout.readline()
			if line == "":
				raise EOFError()

			line = line.rstrip()

			logging.log(ENGINE, "%s >> %s", self.process.pid, line)

			if line:
				return line

	# Parses output from the engine into - Command and the rest       
	def __recv_usi(self):
		command_and_args = self.__recv().split(None, 1)
		if len(command_and_args) == 1:
			return command_and_args[0], ""
		elif len(command_and_args) == 2:
			return command_and_args

	# Tells the engine to switch to usi mode
	def __usi(self):
		self.__send("usi")

		engine_info = {}

		while True:
			command, arg = self.__recv_usi()

			if command == "usiok":
				return engine_info
			elif command == "id":
				name_and_value = arg.split(None, 1)
				if len(name_and_value) == 2:
					engine_info[name_and_value[0]] = name_and_value[1]
			elif command == "Fairy-Stockfish" and " by " in arg:
				# Ignore identification line
				pass
			elif command == "option":
				pass
			else:
				logging.warning("Unexpected engine response to usi: %s %s", command, arg)

	# Waits for the engine to get ready
	def __isready(self):
		self.__send("isready")
		while True:
			command, arg = self.__recv_usi()
			if command == "readyok":
				break
			elif command == "info" and arg.startswith("string "):
				pass
			else:
				logging.warning("Unexpected engine response to isready: %s %s", command, arg)

	# Sends options to the engine in the correct format         
	def __setoption(self, name, value):
		if value is True:
			value = "true"
		elif value is False:
			value = "false"
		elif value is None:
			value = "none"
		self.__send("setoption name %s value %s" % (name, value))

	# lishogi uses chess coordinates internally atm, so we change coords into usi format for the engine
	def __ucitousi(self, moves, string = False):
		transtable = {97: 57, 98: 56, 99: 55, 100: 54, 101: 53, 102: 52, 103: 51, 104: 50, 105: 49 }
		transtable.update({v: k for k, v in transtable.items()})
		if string:
			return moves.translate(transtable)
		return [m.translate(transtable) for m in moves]

	# lishogi used to send pgn role symbol instead of +
	def __fixpromotion(self, moves, string = False):
		newmoves = []
		if string:
			if len(moves) == 5:
				return moves[:4] + '+'
			else:
				return moves
		for m in moves:
			if len(m) == 5:
				newmoves.append(m[:4] + '+')
			else: newmoves.append(m)
		return newmoves 

	# Main function for the engine - tells it to start crunching numbers
	def __go(self, position, moves, movetime=None, clock=None, depth=None, nodes=None):
		self.__send("position fen %s moves %s" % (position, " ".join(moves)))

		builder = []
		builder.append("go")
		if movetime is not None:
			builder.append("movetime")
			builder.append(str(movetime))
		if nodes is not None:
			builder.append("nodes")
			builder.append(str(nodes))
		if depth is not None:
			builder.append("depth")
			builder.append(str(depth))
		if clock is not None:
			builder.append("wtime")
			builder.append(str(max(1, clock["wtime"] * 10)))
			builder.append("btime")
			builder.append(str(max(1, clock["btime"] * 10)))
			builder.append("winc")
			builder.append(str(clock["inc"] * 1000))
			builder.append("binc")
			builder.append(str(clock["inc"] * 1000))

		self.__send(" ".join(builder))

	# Gets us the best move relative to set difficulty
	def __recv_bestmove(self):
		while True:
			command, arg = self.__recv_usi()
			if command == "bestmove":
				bestmove = arg.split()[0]
				if bestmove and bestmove != "(none)":
					return bestmove
				else:
					return None
			elif command == "info":
				continue
			else:
				logging.warning("Unexpected engine response to go: %s %s", command, arg)
	
	def start_stockfish(self):
		# if already running and responding we return
		if self.process and self.process.poll() is None:
			return
		self.process = self.__open_stockfish()
		logging.log(ENGINE, "Started stockfish.")

		self.info = self.__usi()
		self.info.pop("author", None)
		
		self.info["options"] = {}
		self.info["options"]["threads"] = str(self.threads)
		self.info["options"]["hash"] = str(self.memory)
    	#self.info["options"]["analysis contempt"] = "Off"

		for name, value in self.info["options"].items():
			self.__setoption(name, value)
		self.__isready()
		print("Is running")

	def bestmove(self, job):
		lvl = job["work"]["level"]
		moves = job["moves"].split(" ")
		moves = self.__ucitousi(self.__fixpromotion(moves))
		print("Received", moves)
		
		logging.log(ENGINE, "Finding best move...")
		
		self.__setoption("UCI_LimitStrength", lvl < 8)
		self.__setoption("Skill Level", LVL_SKILL[lvl-1])
		self.__setoption("UCI_AnalyseMode", False)
		self.__setoption("MultiPV", 1)
		self.__send("usinewgame")
		self.__isready()
		
		movetime = int(round(LVL_MOVETIMES[lvl - 1] / (self.threads * 0.9 ** (self.threads - 1))))
		
		start = time.time()
		self.__go(job["position"], moves, movetime=movetime, clock=job["work"].get("clock"), depth=LVL_DEPTHS[lvl - 1])
		bestmove = self.__recv_bestmove()
		bestmove = self.__ucitousi(bestmove, True)
		end = time.time()
		
		logging.log(ENGINE, "Played a move in - %0.3fs elapsed", end - start)
		print("Next move: ", bestmove, "\n")
		return bestmove



class Worker:
	def __init__(self):
		self.http = requests.Session()
		self.http.mount("http://", requests.adapters.HTTPAdapter(max_retries=1))
		self.stockfish = Stockfish("stockfish-x86_64", 128, 4)
		self.stockfish.start_stockfish()
		self.job = None

	def getjob(self):
		acquire_header = create_header(self.stockfish.info)
		response = self.http.post(urlparse.urljoin(DEFAULT_ENDPOINT, "acquire"), json=acquire_header, timeout=HTTP_TIMEOUT)
		if response is None or response.status_code == 204:
			logging.debug("No new job for us.")
		elif response.status_code == 202:
			self.job = response.json()
		else:
			logging.error("Unexpected response from server - %d.", response.status_code)

	def sendjob(self, path, req):
		try:
			response = self.http.post(urlparse.urljoin(DEFAULT_ENDPOINT, path), json=req, timeout=HTTP_TIMEOUT)
		except:
			logging.error("Couldn't send.", req)
		else:
			self.job = None
			if response is None or response.status_code == 204:
				logging.debug("No job received.")
			elif response.status_code == 202:
				logging.debug("Received another job.")
				self.job = response.json()
			elif 400 <= response.status_code <= 499:
				logging.error("Client error %d.", response.status_code)
			elif 500 <= response.status_code <= 599:
				logging.error("Server error %d.", response.status_code)

	def work(self):
		while True:
			if self.job and self.job["work"]["type"] == "move":
				result = create_header(self.stockfish.info)
				result["move"] = { "bestmove": self.stockfish.bestmove(self.job), }
				self.sendjob("move" + "/" + self.job["work"]["id"], result)
			else:
				time.sleep(1)
				self.getjob()


def main(argv):
	worker = Worker()
	worker.work()
		




if __name__ == "__main__":
    sys.exit(main(sys.argv))
