"""TeamShift fork checks (enckequity/teamshift#4650).

* who counts as the owner (operator): the machine, the owner's portal, the owner's own direct chat
  listed in owner_ids; never a group chat, another person's chat or a cron run
* every job Steward makes goes through the host's cronjob_manage tool and its "Schedule this job?"
  card (a real-engine check runs when the engine is importable)
* user-facing string literals and the skills name no host application or its apps

Run from the repo root:  python -m unittest discover -s tests -v
"""
import ast
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
steward = __import__(ROOT.name)
core = steward.core

ENGINE = os.environ.get("HERMES_ENGINE_DIR", "/opt/hermes")
OWNER_PHONE = "+15550001111"
MEMBER_PHONE = "+15550002222"
FOUNDER = "relay:" + OWNER_PHONE
ALL_STAFF = "relay:group-all"
OPS_GROUP = "group-ops"
SETUP = dict(company="Acme", timezone="UTC", founder_target=FOUNDER, all_staff_target=ALL_STAFF,
             departments=[{"name": "Ops", "lead": "Ana", "target": "relay:" + OPS_GROUP}])


class FakeCron:
    def __init__(self):
        self.calls, self.n = [], 0

    def __call__(self, name, args):
        self.calls.append((name, dict(args)))
        if args.get("action") == "create":
            self.n += 1
            return json.dumps({"success": True, "job_id": f"job{self.n}"})
        return json.dumps({"success": True})


class Base(unittest.TestCase):
    owner_ids = [f"relay:{OWNER_PHONE}"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env, self.config, self.card = {}, {}, None
        self._saved = (steward._home, steward._get_session_env, steward._load_hermes_config,
                       steward._cron_job_id, steward._card_skip_reason)
        steward._home = lambda: self.tmp
        steward._get_session_env = lambda name: self.env.get(name, "")
        steward._load_hermes_config = lambda: self.config
        steward._cron_job_id = lambda: ""
        steward._card_skip_reason = lambda: self.card
        self.cron = FakeCron()
        test = self

        class Ctx:
            dispatch_tool = staticmethod(self.cron)

            @staticmethod
            def get_config(key, default=None):
                return {"owner_ids": test.owner_ids}.get(key, default)
        steward._CTX = Ctx()

    def tearDown(self):
        (steward._home, steward._get_session_env, steward._load_hermes_config,
         steward._cron_job_id, steward._card_skip_reason) = self._saved
        steward._CTX = None

    def call(self, name, **args):
        return json.loads(steward.HANDLERS[name](args))

    def session(self, platform="relay", chat_id="", chat_type="dm", user_id="", source=""):
        self.env = {"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat_id,
                    "HERMES_SESSION_CHAT_TYPE": chat_type, "HERMES_SESSION_USER_ID": user_id,
                    "HERMES_SESSION_SOURCE": source}

    def owner_dm(self):
        self.session(chat_id=OWNER_PHONE, user_id=OWNER_PHONE)

    def portal(self):
        self.session(platform="api_server", chat_id="portal-chat", chat_type="")

    def state(self):
        return json.loads((self.tmp / "steward" / "state.json").read_text())

    def refused(self, r):
        self.assertFalse(r["success"], r)


class OperatorSurfaceTests(Base):
    def test_who_is_the_owner(self):
        cases = [
            (dict(platform="relay", chat_id=OWNER_PHONE, user_id=OWNER_PHONE), True),
            (dict(platform="RELAY", chat_id=OWNER_PHONE, user_id=OWNER_PHONE.lower()), True),
            (dict(platform="relay", chat_id=OWNER_PHONE, user_id=OWNER_PHONE, chat_type="private"), True),
            (dict(platform="api_server", chat_id="x", chat_type=""), True),
            (dict(platform="", source="tui"), True),
            # never a group, even the owner's own number writing in one
            (dict(platform="relay", chat_id=OPS_GROUP, chat_type="group", user_id=OWNER_PHONE), False),
            (dict(platform="relay", chat_id=OPS_GROUP, chat_type="thread", user_id=OWNER_PHONE), False),
            (dict(platform="api_server", chat_id="x", chat_type="group"), False),
            # the portal with a chat sender bound is not the plain owner portal
            (dict(platform="api_server", chat_id="x", chat_type="", user_id="guest"), False),
            # someone else's direct chat, or the owner's id on another platform
            (dict(platform="relay", chat_id=MEMBER_PHONE, user_id=MEMBER_PHONE), False),
            (dict(platform="photon", chat_id=OWNER_PHONE, user_id=OWNER_PHONE), False),
            # no chat type at all fails closed
            (dict(platform="relay", chat_id=OWNER_PHONE, chat_type="", user_id=OWNER_PHONE), False),
            (dict(platform="webhook", chat_id="x", chat_type=""), False),
            (dict(platform="msgraph_webhook", chat_id="x", chat_type=""), False),
        ]
        for kw, want in cases:
            self.session(**kw)
            self.assertEqual(steward._is_operator(), want, kw)

    def test_cron_is_never_the_owner(self):
        self.owner_dm()
        self.env["HERMES_CRON_SESSION"] = "1"
        self.assertFalse(steward._is_operator())

    def test_no_owner_ids_means_no_chat_owner(self):
        self.owner_ids = []
        self.owner_dm()
        self.assertFalse(steward._is_operator())
        self.owner_ids = "relay:" + OWNER_PHONE   # one string is accepted too
        self.assertTrue(steward._is_operator())
        self.owner_ids = ["relay:", ":x", 5, None]
        self.assertFalse(steward._is_operator())

    def test_first_setup_from_owner_chat_and_portal(self):
        for enter in (self.owner_dm, self.portal):
            (self.tmp / "steward").mkdir(exist_ok=True)
            (self.tmp / "steward" / "state.json").unlink(missing_ok=True)
            self.cron.calls.clear()
            enter()
            r = self.call("steward_setup", **SETUP)
            self.assertTrue(r["success"], (enter.__name__, r))
            self.assertEqual(self.state()["founder_target"], FOUNDER)
            self.assertTrue(self.cron.calls)
            self.assertTrue(all(name == "cronjob_manage" for name, _ in self.cron.calls))

    def test_group_member_never_runs_founder_setup(self):
        # first-time setup from a group: refused, nothing saved, nothing scheduled
        self.session(chat_id=OPS_GROUP, chat_type="group", user_id=MEMBER_PHONE)
        self.refused(self.call("steward_setup", **SETUP))
        self.assertFalse((self.tmp / "steward" / "state.json").exists())
        # the owner's own id inside a group is refused too
        self.session(chat_id=OPS_GROUP, chat_type="group", user_id=OWNER_PHONE)
        self.refused(self.call("steward_setup", **SETUP))
        # once set up, a group member still can't change setup, the founder chat, or publish
        self.owner_dm()
        self.assertTrue(self.call("steward_setup", **SETUP)["success"])
        self.cron.calls.clear()
        for user in (MEMBER_PHONE, OWNER_PHONE):
            self.session(chat_id=OPS_GROUP, chat_type="group", user_id=user)
            self.refused(self.call("steward_setup", departments=[{"name": "HR"}], schedule_jobs=False))
            self.refused(self.call("steward_setup", founder_target="relay:" + MEMBER_PHONE, schedule_jobs=False))
            self.refused(self.call("steward_publish", confirm=True))
            self.refused(self.call("steward_status"))
        self.assertEqual(self.state()["founder_target"], FOUNDER)
        self.assertEqual(self.cron.calls, [])

    def test_member_dm_cannot_setup(self):
        self.session(chat_id=MEMBER_PHONE, user_id=MEMBER_PHONE)
        self.refused(self.call("steward_setup", **SETUP))
        self.assertEqual(self.cron.calls, [])


class ScheduleCardTests(Base):
    def test_jobs_only_through_cronjob_manage(self):
        self.owner_dm()
        self.assertTrue(self.call("steward_setup", **SETUP)["success"])
        self.assertTrue(self.call("steward_publish", confirm=True)["success"])
        names = {name for name, _ in self.cron.calls}
        self.assertEqual(names, {"cronjob_manage"})
        creates = [a for _, a in self.cron.calls if a["action"] == "create"]
        self.assertGreaterEqual(len(creates), 4)
        for a in creates:   # never pre-approved or paused around the card
            self.assertNotIn("approve", a)
            self.assertNotIn("paused", a)

    def test_no_card_surface_saves_settings_but_schedules_nothing(self):
        self.portal()
        self.card = "nobody can answer an approval here"
        r = self.call("steward_setup", **SETUP)
        self.assertTrue(r["success"], r)
        self.assertIn("approve", " ".join(r["notes"]))
        self.assertEqual(self.cron.calls, [])
        self.assertEqual(self.state()["founder_target"], FOUNDER)
        r = self.call("steward_publish", confirm=True)
        self.refused(r)
        self.assertEqual(self.cron.calls, [])

    def test_local_session_without_a_card_schedules_nothing(self):
        self.session(platform="", source="cli", chat_type="")
        self.card = "nobody can answer an approval here"
        r = self.call("steward_setup", **SETUP)
        self.assertTrue(r["success"], r)
        self.assertEqual(self.cron.calls, [])

    def test_allowed_new_jobs_grant_still_schedules(self):
        self.owner_dm()
        self.card = "the user already allowed new jobs"
        self.assertTrue(self.call("steward_setup", **SETUP)["success"])
        self.assertTrue(self.cron.calls)

    def test_no_direct_cron_writes_in_source(self):
        """The plugin never imports the host's cron store or writes jobs.json itself."""
        for path in ROOT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith("cron.jobs"), path)
                    self.assertFalse((node.module or "") == "cron" and any(a.name == "jobs" for a in node.names), path)
                if isinstance(node, ast.Import):
                    self.assertFalse(any(a.name.startswith("cron.jobs") for a in node.names), path)
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotIn("jobs.json", node.value, path)


ENGINE_SCRIPT = textwrap.dedent('''
    import importlib.util, json, os, sys
    plugin, engine, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, engine)
    spec = importlib.util.spec_from_file_location("steward", os.path.join(plugin, "__init__.py"),
                                                  submodule_search_locations=[plugin])
    steward = importlib.util.module_from_spec(spec); sys.modules["steward"] = steward
    spec.loader.exec_module(steward)
    import tools.cronjob_tools  # registers cronjob_manage
    import tools.approval as approval
    from tools.registry import registry
    from gateway.session_context import set_session_vars
    from cron import jobs as cron_jobs

    cards, dispatched = [], []
    def gate(**kw):
        cards.append(kw.get("description", ""))
        return {"approved": mode == "approve", "outcome": "approved" if mode == "approve" else "denied"}
    approval._run_approval_gate = gate
    if mode not in ("portal", "local_q"):
        # a chat surface where the host can ask (the live gateway binds one); the portal keeps the real answer
        approval.schedule_card_skip_reason = lambda **kw: None

    class Ctx:
        @staticmethod
        def dispatch_tool(name, args):
            dispatched.append(name)
            return registry.dispatch(name, args)
        @staticmethod
        def get_config(key, default=None):
            return {"owner_ids": ["relay:+15550001111"]}.get(key, default)
    steward._CTX = Ctx()
    if mode == "portal":
        set_session_vars(platform="api_server", chat_id="portal-chat")
    elif mode == "local_q":
        # a one-shot local run (`chat -q`): the machine itself, but nobody is there to answer a card
        os.environ["HERMES_SINGLE_QUERY_SESSION"] = "1"
        set_session_vars(source="cli")
    else:
        set_session_vars(platform="relay", chat_id="+15550001111", chat_type="dm", user_id="+15550001111")
    r = json.loads(steward.HANDLERS["steward_setup"]({
        "company": "Acme", "timezone": "UTC", "founder_target": "relay:+15550001111",
        "departments": [{"name": "Ops", "target": "relay:group-ops"}]}))
    print(json.dumps({"result": r, "cards": cards, "dispatched": dispatched,
                      "saved": [j.get("name") for j in cron_jobs.load_jobs()]}))
''')


@unittest.skipUnless((Path(ENGINE) / "tools" / "cronjob_tools.py").exists(), "engine not installed")
class RealEngineCardTests(unittest.TestCase):
    """Steward's setup against the engine's own cronjob_manage: each create shows the card."""

    def run_engine(self, mode):
        tmp = Path(tempfile.mkdtemp())
        script = tmp / "probe.py"
        script.write_text(ENGINE_SCRIPT, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
        env["HERMES_HOME"] = str(tmp / "home")
        out = subprocess.run([sys.executable, "-I", str(script), str(ROOT), ENGINE, mode], env=env,
                             capture_output=True, text=True, timeout=240, cwd=str(tmp))
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_declined_card_saves_no_job(self):
        r = self.run_engine("deny")
        self.assertEqual(set(r["dispatched"]), {"cronjob_manage"})
        self.assertEqual(len(r["cards"]), 4, r)   # check-in, nudge, status, pack
        self.assertTrue(all(c.startswith("Schedule \"Steward") for c in r["cards"]), r["cards"])
        self.assertEqual(r["saved"], [])
        self.assertFalse(r["result"]["success"])

    def test_approved_card_saves_jobs(self):
        r = self.run_engine("approve")
        self.assertEqual(len(r["cards"]), 4, r)
        self.assertEqual(len(r["saved"]), 4, r)
        self.assertTrue(r["result"]["success"], r["result"])

    def test_local_one_shot_run_without_a_card_schedules_nothing(self):
        r = self.run_engine("local_q")
        self.assertTrue(r["result"]["success"], r["result"])
        self.assertIn("approve", " ".join(r["result"]["notes"]))
        self.assertEqual(r["dispatched"], [])
        self.assertEqual(r["cards"], [])
        self.assertEqual(r["saved"], [])

    def test_portal_without_a_card_schedules_nothing(self):
        r = self.run_engine("portal")
        self.assertTrue(r["result"]["success"], r["result"])
        self.assertEqual(r["dispatched"], [])
        self.assertEqual(r["cards"], [])
        self.assertEqual(r["saved"], [])


ENGINE_WORDS = re.compile(r"(?i)\b(hermes|nous|cli|tui|desktop app|config\.yaml|allow_agent_scheduling)\b")
# Short tokens that are platform ids compared in code, not shown to anyone.
_TOKENS = re.compile(r"[a-z_]{0,40}")


def _docstrings(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


class NeutralWordingTests(unittest.TestCase):
    def test_no_engine_name_in_string_literals(self):
        bad = []
        for path in sorted(ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = _docstrings(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docs:
                    continue
                text = node.value
                if _TOKENS.fullmatch(text) or text.startswith("HERMES_"):
                    continue
                if ENGINE_WORDS.search(text):
                    bad.append(f"{path.name}:{node.lineno}: {text[:80]!r}")
        self.assertEqual(bad, [])

    def test_skills_are_neutral(self):
        for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
            text = skill.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"(?i)\b(hermes|cli|desktop|computer running)\b", text), skill)

    def test_refusals_name_no_engine(self):
        for msg in (steward.NOT_FOUNDER, steward.NOT_FOUNDER_SETUP, steward.NOT_FOUNDER_PACK,
                    steward.FOUNDER_TARGET_LOCAL_ONLY, steward.NO_SCHEDULE_CARD, steward.files.OUTSIDE_MEDIA):
            self.assertIsNone(ENGINE_WORDS.search(msg), msg)


if __name__ == "__main__":
    unittest.main()
