# -*- coding: utf-8 -*-
# The add-on's own settings, in elevenlabs_settings.json in NVDA's configuration
# folder. The API keys are encrypted with DPAPI, for the current Windows user only.
#
# The leading underscore keeps NVDA from listing this module as a synthesizer.

import base64
import copy
import ctypes
import json
import os
import threading
from contextlib import contextmanager
from ctypes import wintypes

import globalVars
from logHandler import log

SETTINGS_FILENAME = "elevenlabs_settings.json"

_lock = threading.RLock()
# The parsed file, loaded on first use. Only this module writes the file.
_data = None
# Decrypted keys by id, cached; None for one that can't be decrypted.
_apiKeys = {}
# What is learned about an account, kept per key, in "accounts".
_ACCOUNT_SETTINGS = ("outputFormat", "voiceChecks", "modelCheck", "refusedFields")


def settingsPath():
	return os.path.join(globalVars.appArgs.configPath, SETTINGS_FILENAME)


def _load():
	global _data
	if _data is not None:
		return _data
	path = settingsPath()
	try:
		with open(path, "r", encoding="utf-8") as f:
			data = json.load(f)
		if not isinstance(data, dict):
			raise ValueError("not a JSON object")
	except FileNotFoundError:
		data = {}
	except (OSError, ValueError):
		log.error(f"ElevenLabs: {path} could not be read and is ignored.", exc_info=True)
		# Kept aside rather than overwritten by the next save.
		try:
			os.replace(path, path + ".bad")
		except OSError:
			pass
		data = {}
	if _migrate(data):
		_save(data)
	_data = data
	return _data


def _migrate(data):
	"""Moves the single key of earlier versions, and what was learned about its
	account, into the list of keys; True if anything changed."""
	legacy = data.pop("apiKey", None)
	learned = {name: data.pop(name) for name in _ACCOUNT_SETTINGS if name in data}
	if not legacy:
		# Without its key, what was learned belongs to no account.
		return bool(learned)
	keyId = _newKeyId(data)
	# Named by the user later; the settings show a default meanwhile.
	data.setdefault("apiKeys", []).append({"id": keyId, "name": "", "key": legacy})
	if _activeKeyId(data) is None:
		data["activeKey"] = keyId
	if learned:
		data.setdefault("accounts", {})[keyId] = learned
	return True


def _save(data):
	"""Writes data to the file in one step, so a crash can't leave half a file."""
	path = settingsPath()
	tempPath = path + ".tmp"
	try:
		with open(tempPath, "w", encoding="utf-8") as f:
			json.dump(data, f, indent="\t", sort_keys=True)
			f.write("\n")
		os.replace(tempPath, path)
		return True
	except OSError:
		log.error(f"ElevenLabs: could not save {path}", exc_info=True)
		return False


def get(name, default=None):
	"""A copy of a top level setting, so changing it changes nothing saved."""
	with _lock:
		return copy.deepcopy(_load().get(name, default))


@contextmanager
def editing():
	"""Yields the settings to change in place, then saves them. Edits take turns."""
	global _data
	with _lock:
		data = copy.deepcopy(_load())
		yield data
		if data != _data and _save(data):
			_data = data


def invalidateChecks():
	"""Forgets the recorded voice and model checks of every account, so the next
	load redoes them.

	For when the synthesizer is not running and there is nothing to check against."""
	global _data
	with _lock:
		data = copy.deepcopy(_load())
		for learned in data.get("accounts", {}).values():
			learned.pop("voiceChecks", None)
			learned.pop("modelCheck", None)
		if data == _data:
			return True
		if not _save(data):
			return False
		_data = data
		return True


# --- API key ---


class _DataBlob(ctypes.Structure):
	_fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char)))


# Own handles, so the argtypes set here can't clash with ctypes.windll users.
_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_blobArgs = (
	ctypes.POINTER(_DataBlob),
	wintypes.LPCWSTR,
	ctypes.POINTER(_DataBlob),
	ctypes.c_void_p,
	ctypes.c_void_p,
	wintypes.DWORD,
	ctypes.POINTER(_DataBlob),
)
_crypt32.CryptProtectData.argtypes = _blobArgs
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = (
	ctypes.POINTER(_DataBlob),
	ctypes.POINTER(wintypes.LPWSTR),
) + _blobArgs[2:]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
_kernel32.LocalFree.restype = ctypes.c_void_p

# Never show Windows' own prompts.
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Ties the encrypted key to this add-on, so other DPAPI data can't be mistaken for it.
_ENTROPY = b"ElevenLabsTTS NVDA add-on API key"


def _blob(data):
	buffer = ctypes.create_string_buffer(data, len(data))
	return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt(function, data, *extra):
	"""Runs CryptProtectData or CryptUnprotectData on data and returns the result."""
	dataIn, _keepIn = _blob(data)
	entropy, _keepEntropy = _blob(_ENTROPY)
	dataOut = _DataBlob()
	if not function(
		ctypes.byref(dataIn), *extra, ctypes.byref(entropy), None, None,
		_CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(dataOut),
	):
		raise ctypes.WinError(ctypes.get_last_error())
	try:
		return ctypes.string_at(dataOut.pbData, dataOut.cbData)
	finally:
		_kernel32.LocalFree(ctypes.cast(dataOut.pbData, ctypes.c_void_p))


def _keys(data):
	"""The saved keys in data, in the order they were added."""
	return [
		k for k in data.get("apiKeys", ())
		if isinstance(k, dict) and isinstance(k.get("id"), str) and k.get("key")
	]


def _activeKeyId(data):
	active = data.get("activeKey")
	return active if any(k["id"] == active for k in _keys(data)) else None


def _newKeyId(data):
	used = {k["id"] for k in _keys(data)}
	while True:
		# Not secrets.token_hex: older NVDA versions don't bundle the secrets module.
		keyId = os.urandom(4).hex()
		if keyId not in used:
			return keyId


def apiKeys():
	"""The saved keys as (id, name), in the order they were added."""
	with _lock:
		return [(k["id"], k.get("name") or "") for k in _keys(_load())]


def activeKeyId():
	"""The id of the key in use, or None if there is none."""
	with _lock:
		return _activeKeyId(_load())


def getApiKey(keyId=None):
	"""A saved key, by default the one in use; None if there is none or it can't be decrypted."""
	with _lock:
		if keyId is None:
			keyId = activeKeyId()
			if keyId is None:
				return None
		if keyId in _apiKeys:
			return _apiKeys[keyId]
		saved = next((k["key"] for k in _keys(_load()) if k["id"] == keyId), None)
		if saved is None:
			return None
		key = None
		try:
			key = _crypt(_crypt32.CryptUnprotectData, base64.b64decode(saved), None).decode("utf-8")
		except (OSError, ValueError):
			# E.g. a configuration copied from another computer or user.
			log.warning(
				"ElevenLabs: a saved API key can't be decrypted by this Windows user "
				"on this computer. Add it again in NVDA's settings.",
				exc_info=True,
			)
		_apiKeys[keyId] = key
		return key


def findApiKey(key):
	"""The id of the saved key equal to key, or None."""
	with _lock:
		return next((keyId for keyId, name in apiKeys() if getApiKey(keyId) == key), None)


def addApiKey(name, key):
	"""Encrypts and saves key under name, putting it in use if no other key is;
	its id, or None if it couldn't be saved."""
	try:
		encrypted = _crypt(_crypt32.CryptProtectData, key.encode("utf-8"), None)
	except OSError:
		log.error("ElevenLabs: could not encrypt the API key", exc_info=True)
		return None
	with _lock:
		with editing() as data:
			keyId = _newKeyId(data)
			data.setdefault("apiKeys", []).append({
				"id": keyId,
				"name": name,
				"key": base64.b64encode(encrypted).decode("ascii"),
			})
			if _activeKeyId(data) is None:
				data["activeKey"] = keyId
		if not any(k["id"] == keyId for k in _keys(_data)):
			return None
		_apiKeys[keyId] = key
		return keyId


def renameApiKey(keyId, name):
	"""Renames a saved key; False if the file couldn't be saved."""
	with _lock:
		with editing() as data:
			for k in _keys(data):
				if k["id"] == keyId:
					k["name"] = name
		return any(k["id"] == keyId and k.get("name") == name for k in _keys(_data))


def removeApiKey(keyId):
	"""Removes a key and what was learned about its account. If it was in use, the
	first key left is used instead. False if the file couldn't be saved."""
	with _lock:
		with editing() as data:
			data["apiKeys"] = [k for k in _keys(data) if k["id"] != keyId]
			data.get("accounts", {}).pop(keyId, None)
			if _activeKeyId(data) is None:
				remaining = _keys(data)
				if remaining:
					data["activeKey"] = remaining[0]["id"]
				else:
					data.pop("activeKey", None)
		removed = not any(k["id"] == keyId for k in _keys(_data))
		if removed:
			_apiKeys.pop(keyId, None)
		return removed


def setActiveKey(keyId):
	"""Puts a saved key in use; False if it couldn't be."""
	with _lock:
		with editing() as data:
			if any(k["id"] == keyId for k in _keys(data)):
				data["activeKey"] = keyId
		return _activeKeyId(_data) == keyId


# --- What is learned about each account ---


def getAccount(keyId, name, default=None):
	"""A copy of something learned about the account of key keyId."""
	with _lock:
		return copy.deepcopy(_load().get("accounts", {}).get(keyId, {}).get(name, default))


@contextmanager
def editingAccount(keyId):
	"""Yields what was learned about the account of key keyId to change in place, then
	saves it. Changes for a key that has been removed are dropped."""
	with editing() as data:
		if not any(k["id"] == keyId for k in _keys(data)):
			yield {}
			return
		yield data.setdefault("accounts", {}).setdefault(keyId, {})
