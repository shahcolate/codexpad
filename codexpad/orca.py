#!/usr/bin/env python3
"""codexpad <-> Orca bridge - the pad follows an Orca fleet, and answers it.

Orca (https://www.onorca.dev) is an agent development environment: it runs a
fleet of coding agents -- Claude Code, Codex, Gemini, Cursor, Copilot, Amp,
Droid and a dozen more CLIs -- each in its own git worktree, and it collects
every one of their statuses through hooks it installs itself.

codexpad already lights one Agent Key per working directory, and an Orca
worktree IS a working directory, so the two line up with no bookkeeping at
all: whatever Orca knows about a worktree lands on the key that worktree
already owns. A Claude session inside Orca keeps its own hook feed (which is
finer-grained -- per tool call); every other agent in the fleet gets its key
from here. Same six keys, same amber, no second mapping to reason about.

Transport
---------
The running Orca app writes `orca-runtime.json` into its userData directory
with a unix socket path and a shared auth token. A request is one JSON object
on one line:

    {"id": "<uuid>", "authToken": "...", "method": "worktree.ps", "params": {}}

and the reply comes back the same way -- {"id", "ok": true, "result"} or
{"id", "ok": false, "error"} -- with {"_keepalive": true} frames interleaved
while a long call is in flight. That is the entire protocol, so this is a
small stdlib-only client for it, in the same spirit as the rest of codexpad.

Read against Orca 1.4.x (`src/cli/runtime/transport.ts`, `src/main/runtime/
runtime-rpc.ts`, MIT). Orca is unaffiliated with codexpad; a future version
may rename a method, and the bridge fails quiet and keeps the pad on hooks if
it does.
"""
import json
import os
import socket
import sys
import uuid

from . import config

METADATA_FILE = "orca-runtime.json"


class OrcaError(Exception):
    """Any failure talking to the Orca runtime. Carries a short code."""

    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def user_data_path():
    """Where the Orca app keeps its userData directory.

    Mirrors Orca's own resolution (`getDefaultUserDataPath`), including the
    ORCA_USER_DATA_PATH override that dev builds and parallel instances use.
    config._home() rather than ~ so a sudo'd daemon looks in the real user's
    home instead of /var/root.
    """
    override = os.environ.get("ORCA_USER_DATA_PATH")
    if override:
        return override
    home = config._home()
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "orca")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return os.path.join(appdata, "orca") if appdata else ""
    return os.path.join(os.environ.get("XDG_CONFIG_HOME")
                        or os.path.join(home, ".config"), "orca")


def metadata_path():
    base = user_data_path()
    return os.path.join(base, METADATA_FILE) if base else ""


def read_metadata():
    """The live runtime's socket + token, or None when Orca isn't running.

    Orca rewrites this file on every launch and sweeps the socket on exit, so
    "file missing or unparsable" is the normal not-running answer, not an
    error worth shouting about.
    """
    path = metadata_path()
    if not path:
        return None
    try:
        with open(path) as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not meta.get("authToken"):
        return None
    for transport in (meta.get("transports") or []):
        if isinstance(transport, dict) and transport.get("kind") == "unix" \
                and transport.get("endpoint"):
            meta["endpoint"] = transport["endpoint"]
            return meta
    legacy = meta.get("transport")      # pre-transports-array metadata
    if isinstance(legacy, dict) and legacy.get("kind") == "unix" \
            and legacy.get("endpoint"):
        meta["endpoint"] = legacy["endpoint"]
        return meta
    return None


class Orca:
    """One connection-per-call client for the Orca runtime socket.

    Orca closes the socket after each reply, so there is no connection to
    keep alive and nothing to reconnect: every call stands alone. A dead app
    surfaces as OrcaError('runtime_unavailable'), which the bridge treats as
    "Orca isn't running right now" and retries later.
    """

    def __init__(self, metadata):
        self.metadata = metadata
        self.endpoint = metadata["endpoint"]
        self.runtime_id = metadata.get("runtimeId")

    def call(self, method, params=None, timeout=10.0):
        request_id = str(uuid.uuid4())
        payload = json.dumps({"id": request_id,
                              "authToken": self.metadata["authToken"],
                              "method": method,
                              "params": {} if params is None else params})
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            try:
                sock.connect(self.endpoint)
                sock.sendall(payload.encode() + b"\n")
            except OSError as exc:
                raise OrcaError("runtime_unavailable", str(exc))
            buf = b""
            while True:
                try:
                    chunk = sock.recv(65536)
                except socket.timeout:
                    raise OrcaError("runtime_timeout",
                                    f"{method} did not answer in {timeout}s")
                except OSError as exc:
                    raise OrcaError("runtime_unavailable", str(exc))
                if not chunk:
                    raise OrcaError("runtime_unavailable",
                                    "runtime closed the connection")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        raise OrcaError("invalid_runtime_response",
                                        "runtime sent a non-JSON frame")
                    # keepalives keep a long call's socket warm; they carry
                    # nothing and are not the terminal frame
                    if frame.get("_keepalive"):
                        continue
                    if frame.get("id") != request_id:
                        continue
                    if frame.get("ok"):
                        return frame.get("result")
                    err = frame.get("error") or {}
                    raise OrcaError(err.get("code", "runtime_error"),
                                    err.get("message", "unknown error"))
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # --- the four calls the bridge actually makes ---------------------------
    def ps(self, after_snapshot_id=None, limit=None, timeout=10.0):
        """worktree.ps: every managed worktree with its live agent rows.

        afterSnapshotId is Orca's conditional-fetch cursor -- pass the id from
        the previous answer and an unchanged fleet replies {"unchanged": true}
        instead of the whole catalogue. It is sent on EVERY call, null when we
        have no cursor yet: omitting the field entirely is how a caller asks
        for the pre-cursor response shape, which carries no snapshotId at all
        and would leave the poll fetching the whole catalogue forever. Older
        runtimes ignore the field and return the full result, which reads the
        same to the caller.
        """
        params = {"afterSnapshotId": after_snapshot_id or None}
        if limit:
            params["limit"] = limit
        return self.call("worktree.ps", params, timeout=timeout)

    def activate(self, worktree_id, timeout=5.0):
        """Bring a worktree to the front in the Orca window."""
        return self.call("worktree.activate",
                         {"worktree": f"id:{worktree_id}", "navigation": "host"},
                         timeout=timeout)

    def pane_handle(self, pane_key, worktree_id, timeout=5.0):
        """The terminal handle behind one agent's pane, or None."""
        try:
            result = self.call("terminal.resolvePane",
                               {"paneKey": pane_key, "worktreeId": worktree_id},
                               timeout=timeout) or {}
        except OrcaError:
            return None
        return (result.get("terminal") or {}).get("handle")

    def active_handle(self, worktree_id, timeout=5.0):
        """The worktree's active terminal handle, or None."""
        try:
            result = self.call("terminal.resolveActive",
                               {"worktree": f"id:{worktree_id}"},
                               timeout=timeout) or {}
        except OrcaError:
            return None
        return result.get("handle")

    def focus_terminal(self, handle, timeout=5.0):
        return self.call("terminal.focus",
                         {"terminal": handle, "navigation": "host"},
                         timeout=timeout)

    def send(self, handle, text="", enter=False, timeout=5.0):
        """Type into one agent's terminal - the precise answer path.

        This is what makes the pad's check/cross honest under Orca: instead of
        keystrokes aimed at whatever window happens to be focused, the Enter
        goes into the pane of the agent that is actually asking.
        """
        return self.call("terminal.send",
                         {"terminal": handle, "text": text, "enter": bool(enter),
                          "client": {"id": "codexpad", "type": "desktop"}},
                         timeout=timeout)


# --- Orca status -> codexpad state ------------------------------------------
# Orca aggregates each worktree's panes into one status, escalating in the
# same order the pad cares about: inactive < active < done < working <
# permission (orca-runtime.ts, WORKTREE_STATUS_PRIORITY). So the worktree
# status IS the key colour, with one refinement below for 'active'.
STATUS_MAP = {
    "permission": "blocked",     # an agent is waiting on YOU -- the amber
    "working": "working",
    "done": "done",
    "active": "idle",            # a live worktree with nothing running
    "inactive": None,            # asleep: not worth a key
}

# Per-agent rows carry a fourth state the worktree roll-up does not:
# 'blocked' (needs the user) vs 'waiting' (a coordinator waiting on its own
# subagent -- still busy, not your problem).
AGENT_STATE_MAP = {"blocked": "blocked", "working": "working",
                   "waiting": "working", "done": "done"}


def pad_state(summary):
    """The state one worktree should paint, or None to not claim a key.

    A worktree with no agent in it is deliberately NOT worth a key: an Orca
    user can have twenty worktrees open and there are six keys. Only agent
    activity claims one, so the pad keeps showing agents rather than folders.
    """
    if summary.get("isArchived"):
        return None
    status = summary.get("status")
    agents = [a for a in (summary.get("agents") or []) if isinstance(a, dict)]
    if status == "active":
        # 'active' means no agent is running now -- but an agent that just
        # finished still has a row saying 'done', and that green (turn
        # finished, unacknowledged) is the whole point of the key.
        if any(AGENT_STATE_MAP.get(a.get("state")) == "done" for a in agents):
            return "done"
        return "idle" if agents else None
    return STATUS_MAP.get(status)


def blocked_pane(summary):
    """(paneKey, agentType) of the agent asking for something, or (None, None).

    Rows arrive newest-state-first, so the first blocked row is the one that
    just asked -- the one the check key should answer.
    """
    for agent in (summary.get("agents") or []):
        if isinstance(agent, dict) and agent.get("state") == "blocked":
            return agent.get("paneKey"), agent.get("agentType")
    return None, None


def activity_token(summary):
    """A value that changes whenever a working agent does something new.

    Orca reports the running tool per agent, so a change here means a tool
    call happened -- the same thing codexpad's PreToolUse hook shimmers on,
    available for every agent in the fleet rather than only Claude.
    """
    marks = []
    for agent in (summary.get("agents") or []):
        if isinstance(agent, dict):
            marks.append((agent.get("paneKey"), agent.get("toolName"),
                          agent.get("updatedAt")))
    return tuple(marks)


def probe():
    """One-shot description of the Orca side, for `codexpad.check` and status.

    Never raises: every failure is a sentence in the returned dict.
    """
    out = {"metadata": metadata_path(), "running": False, "reachable": False,
           "worktrees": 0, "agents": 0, "active": [], "error": ""}
    meta = read_metadata()
    if not meta:
        out["error"] = "no Orca runtime metadata (is the Orca app running?)"
        return out
    out["running"] = True
    try:
        result = Orca(meta).ps(timeout=5.0) or {}
    except OrcaError as exc:
        out["error"] = exc.message
        return out
    out["reachable"] = True
    worktrees = result.get("worktrees") or []
    out["worktrees"] = len(worktrees)
    for summary in worktrees:
        if not isinstance(summary, dict):
            continue
        out["agents"] += len(summary.get("agents") or [])
        state = pad_state(summary)
        if state:
            out["active"].append({"path": summary.get("path"),
                                  "name": summary.get("displayName"),
                                  "status": summary.get("status"),
                                  "state": state})
    return out


def main():
    """`python -m codexpad.orca` - print what the bridge can see right now."""
    info = probe()
    print(f"metadata  {info['metadata'] or '(unknown userData path)'}")
    if not info["running"]:
        print(f"runtime   not running — {info['error']}")
        return 1
    if not info["reachable"]:
        print(f"runtime   metadata present but unreachable — {info['error']}")
        return 1
    print(f"runtime   reachable — {info['worktrees']} worktrees, "
          f"{info['agents']} live agents")
    if not info["active"]:
        print("keys      nothing would light: no agent activity right now")
        return 0
    print("keys      what the pad would show:")
    for row in info["active"]:
        print(f"          {row['state']:<8} {row['status']:<11} "
              f"{row['name'] or ''} {row['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
