from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import settings
from watcher import autofix
from watcher import events as ev

ME, REV = "me", "plaxdan"
H, C, M = "a" * 40, "c" * 40, "e" * 40
URL = "https://github.com/acme/api/pull/7"


def comment(cid, author, body="**Nit:** rename `t` to `total`", commit=H, bot=False):
    return {"id": cid, "author": author, "is_bot": bot, "created_at": "2026-11-06T10:00:00Z",
            "url": f"{URL}#discussion_r{cid}", "body": body, "commit": commit,
            "original_commit": commit, "review_id": 1, "review_state": "COMMENTED"}


def thread(tid, *comments, resolved=False, path="src/total.py"):
    return {"id": tid, "resolved": resolved, "outdated": False, "resolved_by": None,
            "path": path, "line": 12, "original_line": 12, "comments": list(comments)}


def snap(threads=None, head=H, state="OPEN", author=ME, reviews=None, requests=()):
    return {"id": "PR1", "kind": "mine", "repo": "api", "number": 7,
            "title": "ABC-123: total the cart", "url": URL, "state": state, "author": author,
            "head": head, "head_ref": "feature", "base_ref": "main",
            "updated_at": "2026-11-06T10:00:00Z", "approved_by": [],
            "reviews": reviews if reviews is not None else [
                {"id": 1, "state": "COMMENTED", "submitted_at": "2026-11-06T10:00:00Z",
                 "body": "", "url": "", "author": REV, "is_bot": False, "commit": H}],
            "issue_comments": [], "review_requests": list(requests),
            "threads": threads if threads is not None else [thread("T1", comment(11, REV))]}


def config(tmp: Path, **autofix_cfg) -> dict:
    cfg = copy.deepcopy(settings.DEFAULTS)
    cfg.update(me=ME, org="acme", workspace=str(tmp / "ws"), repos_root=str(tmp / "ws"))
    cfg["autofix"].update(enabled=True, dry_run=False, validate={"api": "make test"},
                          worktrees_dir=str(tmp / "worktrees"))
    cfg["autofix"].update(autofix_cfg)
    cfg["notify"]["sounds"] = True
    return cfg


def opened(tid="T1", cid=11, body="**Nit:** rename `t` to `total`"):
    return {"thread_id": tid, "comment_id": cid, "author": REV, "body": body,
            "url": f"{URL}#discussion_r{cid}", "path": "src/total.py", "line": 12,
            "commit": H, "original_commit": H, "kind": "nit"}


def proc(argv, rc=0, out="", err=""):
    return subprocess.CompletedProcess(argv, rc, out, err)


class Fake:
    """Stands in for every command a run makes. `local` is git, claude and the
    validate command; `remote` is the push and every GitHub write."""

    def __init__(self, assessment=None, edit=None, numstat="1\t1\tsrc/total.py\n",
                 validate_rc=0, behind=False, merge_rc=0, remote_head=H, push_rc=0,
                 reply_rc=0):
        self.assessment = assessment or {"mechanical": True, "agrees": True, "confidence": 0.95,
                                         "files": ["src/total.py"],
                                         "plan": "Rename t to total.", "reason": "Clearer."}
        self.edit = edit or {"changed": True, "summary": "renamed `t` to `total`"}
        self.numstat, self.validate_rc, self.behind = numstat, validate_rc, behind
        self.merge_rc, self.remote_head, self.push_rc, self.reply_rc = (
            merge_rc, remote_head, push_rc, reply_rc)
        self.head = H
        self.calls, self.remote_calls, self.notes, self.prompts = [], [], [], []

    def local(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "claude":
            self.prompts.append(kw.get("input", ""))
            editing = any(a.startswith("Edit(") for a in argv)
            result = json.dumps(self.edit if editing else self.assessment)
            return proc(argv, out=json.dumps({"result": result, "total_cost_usd": 0.01}))
        if argv[0] == "/bin/sh":
            return proc(argv, self.validate_rc, "compiling\n", "" if not self.validate_rc
                        else "error: test failed\n")
        if argv[0] == "gh":
            return proc(argv, out="PR_node\n")
        sub = argv[1]
        if sub == "clone":
            Path(argv[-1], ".git").mkdir(parents=True)
        elif sub == "remote":
            return proc(argv, out="git@github.com:acme/api.git\n")
        elif sub == "rev-parse":
            return proc(argv, out=self.head + "\n")
        elif sub == "diff" and "--numstat" in argv:
            return proc(argv, out=self.numstat)
        elif sub == "diff":
            return proc(argv, out="diff --git a/src/total.py b/src/total.py\n")
        elif sub == "commit":
            self.head = C
        elif sub == "merge-base":
            return proc(argv, 1 if self.behind else 0)
        elif sub == "merge" and "--abort" not in argv:
            if self.merge_rc == 0:
                self.head = M
            return proc(argv, self.merge_rc, "CONFLICT (content)\n")
        elif sub == "ls-remote":
            return proc(argv, out=f"{self.remote_head}\trefs/heads/feature\n")
        return proc(argv)

    def remote(self, argv, **kw):
        self.remote_calls.append(list(argv))
        if argv[:2] == ["git", "push"]:
            return proc(argv, self.push_rc, "", "rejected" if self.push_rc else "")
        if "graphql" in argv:
            return proc(argv, out="true\n")
        if any("/replies" in a for a in argv):
            return proc(argv, self.reply_rc, f"{URL}#discussion_r99\n",
                        "HTTP 422" if self.reply_rc else "")
        return proc(argv)

    def notify(self, argv, **kw):
        self.notes.append(list(argv))
        return proc(argv, 1 if argv[0] == "pgrep" else 0)

    def git_subs(self):
        return [c[1] for c in self.calls if c[0] == "git"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "ws").mkdir()
        patcher = unittest.mock.patch.object(autofix, "AUTOFIX_DIR", self.tmp / "jobs")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = config(self.tmp)

    def job(self, **kw):
        j = autofix.new_job(snap(), opened(), self.cfg)
        j.update(kw)
        return j

    def tools(self, fake, snaps=None, jobs=()):
        snaps = list(snaps or [snap(), snap(threads=[thread(
            "T1", comment(11, REV), comment(99, ME, "Done"), resolved=True)])])
        def fetch(pr, cfg):
            return snaps.pop(0) if len(snaps) > 1 else snaps[0]
        return autofix.Tools(run=fake.local, remote=fake.remote, fetch_pr=fetch,
                             notify_run=fake.notify, alive=lambda pid: False,
                             jobs=lambda: list(jobs))

    def run_fix(self, fake, job=None, dry_run=False, **kw):
        job = job or self.job()
        rc = autofix.Fix(job, self.cfg, self.tools(fake, **kw), dry_run=dry_run).run()
        return rc, job


class CandidatesTest(unittest.TestCase):
    def test_only_the_configured_kinds_qualify(self):
        threads = [opened(body="**Nit:** x"), opened(body="Suggestion: y"),
                   opened(body="Why is this here?"), opened(body="```suggestion\nz\n```"),
                   opened(body="**Blocking:** no")]
        got = autofix.candidates(threads, ["nit", "suggestion"])
        self.assertEqual([t["kind"] for t in got], ["nit", "suggestion", "suggestion"])
        self.assertEqual(autofix.candidates(threads, ["nit"])[0]["kind"], "nit")


class OpenedThreadsTest(unittest.TestCase):
    def pr(self, threads, kind="mine"):
        return {**snap(threads=threads), "kind": kind}

    def test_a_new_human_comment_opening_a_thread_counts_once(self):
        _, st = ev.diff_pr(self.pr([]), None, ev.Ctx(me=ME, seeding=True))
        p = self.pr([thread("T1", comment(11, REV)), thread("T2", comment(12, ME))])
        got = ev.opened_threads(p, st, ev.Ctx(me=ME))
        self.assertEqual([(t["thread_id"], t["comment_id"], t["path"]) for t in got],
                         [("T1", 11, "src/total.py")])
        _, st2 = ev.diff_pr(p, st, ev.Ctx(me=ME))
        self.assertEqual(ev.opened_threads(p, st2, ev.Ctx(me=ME)), [])

    def test_replies_bots_resolved_threads_seeding_and_others_prs_dont(self):
        _, st = ev.diff_pr(self.pr([thread("T1", comment(11, REV))]), None,
                           ev.Ctx(me=ME, seeding=True))
        p = self.pr([thread("T1", comment(11, REV), comment(13, REV, "also")),
                     thread("T3", comment(14, "bot", bot=True)),
                     thread("T4", comment(15, REV), resolved=True)])
        self.assertEqual(ev.opened_threads(p, st, ev.Ctx(me=ME)), [])
        self.assertEqual(ev.opened_threads(p, None, ev.Ctx(me=ME, seeding=True)), [])
        self.assertEqual(ev.opened_threads(self.pr([thread("T9", comment(16, REV))], "others"),
                                           st, ev.Ctx(me=ME)), [])


class JobStoreTest(Base):
    def test_a_comment_queues_once_and_helper_files_arent_jobs(self):
        jid = autofix.enqueue(snap(), opened(), self.cfg)
        self.assertEqual(jid, autofix.job_id("T1", 11))
        self.assertIsNone(autofix.enqueue(snap(), opened(), self.cfg))
        (autofix.AUTOFIX_DIR / f"{jid}.reply.md").write_text("x")
        (autofix.AUTOFIX_DIR / f"{jid}.extra.json").write_text("{}")
        [job] = autofix.all_jobs()
        self.assertEqual((job["status"], job["reviewer"], job["kind"], job["pr"]["head_ref"]),
                         ("queued", REV, "nit", "feature"))

    def test_the_follow_up_job_list_never_sees_autofix_jobs(self):
        from watcher import followup
        with unittest.mock.patch.object(followup, "JOBS_DIR", self.tmp / "fjobs"), \
                unittest.mock.patch.object(autofix, "AUTOFIX_DIR", self.tmp / "fjobs" / "autofix"):
            autofix.enqueue(snap(), opened(), self.cfg)
            self.assertEqual(followup.all_jobs(), [])
            self.assertEqual(len(autofix.all_jobs()), 1)

    def test_cleared_when_you_answer_or_the_pr_closes(self):
        j = self.job()
        mine_reply = snap(threads=[thread("T1", comment(11, REV), comment(20, ME, "ok"))])
        self.assertEqual(autofix.cleared_jobs(mine_reply, [j], ME),
                         [(j["id"], "you replied in the thread")])
        resolved = snap(threads=[thread("T1", comment(11, REV), resolved=True)])
        self.assertEqual(autofix.cleared_jobs(resolved, [j], ME),
                         [(j["id"], "the thread is resolved")])
        self.assertEqual(autofix.cleared_jobs(snap(), [j], ME), [])
        self.assertEqual(autofix.cleared_jobs(resolved, [{**j, "status": "done"}], ME), [])
        self.assertEqual(autofix.orphaned_prs([j], {"PR2"}), {"PR1"})
        autofix.save_job(j)
        self.assertEqual(autofix.cancel_for_pr("PR1", "MERGED", self.cfg), [j["id"]])
        self.assertEqual(autofix.load_job(j["id"])["cleared"], "the PR merged")


class PureChecksTest(Base):
    def test_expected_head_follows_this_pipelines_own_pushes(self):
        j = self.job()
        pushed = [{**self.job(), "id": "x1", "pushed_from": H, "pushed_sha": C},
                  {**self.job(), "id": "x2", "pushed_from": C, "pushed_sha": M}]
        self.assertEqual(autofix.expected_head(j, pushed), M)
        self.assertEqual(autofix.expected_head(j, []), H)

    def test_preconditions(self):
        j, cfg = self.job(), self.cfg
        check = lambda s, jobs=(): autofix.preconditions(j, s, list(jobs), cfg, lambda p: True)  # noqa: E731
        self.assertEqual(check(snap()), ("go", ""))
        self.assertEqual(check(None)[0], "escalate")
        self.assertEqual(check(snap(state="MERGED")), ("clear", "the PR merged"))
        self.assertEqual(check(snap(author="someone"))[1], "the PR isn't yours")
        self.assertEqual(check(snap(head="f" * 40))[1], "the PR moved since the comment")
        self.assertEqual(check(snap(threads=[thread("T1", comment(11, REV), resolved=True)])),
                         ("clear", "the thread is resolved"))
        self.assertEqual(check(snap(threads=[thread("T1", comment(11, REV, "Why this?"))]))[0],
                         "clear")
        older = autofix.preconditions({**j, "comment_commit": "b" * 40}, snap(), [], cfg)
        self.assertEqual(older[1], "the comment is on an older commit than the PR's head")
        cfg["autofix"]["validate"] = {}
        self.assertEqual(check(snap())[1], "api has no [autofix.validate] command")
        cfg["autofix"]["validate"] = {"api": "make test"}
        running = {**self.job(), "id": "other", "status": "running", "pid": 123}
        self.assertEqual(check(snap(), [running])[1], "another auto-fix is running on this PR")
        main_branch = {**snap(), "head_ref": "main"}
        self.assertEqual(check(main_branch)[0], "escalate")

    def test_assessment_needs_literal_trues_and_a_real_confidence(self):
        a = autofix.parse_assessment('```json\n{"mechanical": "yes", "agrees": true, '
                                     '"confidence": 0.95, "files": ["a.py", 3], "plan": "x"}\n```')
        self.assertEqual((a["mechanical"], a["agrees"], a["files"]), (False, True, ["a.py"]))
        for bad in ('{"confidence": true}', '{"confidence": 1.5}', '{"confidence": "0.9"}', "no"):
            with self.assertRaises(ValueError):
                autofix.parse_assessment(bad)

    def test_decide(self):
        a = autofix.parse_assessment('{"mechanical": true, "agrees": true, "confidence": 0.95, '
                                     '"plan": "Rename it.", "reason": "Clear."}')
        self.assertIsNone(autofix.decide(a, 0.9))
        self.assertTrue(autofix.decide({**a, "agrees": False}, 0.9).startswith("Claude disagrees"))
        self.assertTrue(autofix.decide({**a, "mechanical": False}, 0.9).startswith("it isn't a mechanical"))
        self.assertIn("only 85% sure", autofix.decide({**a, "confidence": 0.85}, 0.9))
        self.assertEqual(autofix.decide({**a, "plan": ""}, 0.9), "the assessment gave no plan for the fix")

    def test_scope(self):
        self.assertEqual(autofix.parse_numstat("3\t1\ta.py\n-\t-\tlogo.png\n"),
                         {"a.py": 4, "logo.png": None})
        ok = {"a.py"}
        self.assertIsNone(autofix.scope_problem({"a.py": 4}, ok, 40))
        self.assertEqual(autofix.scope_problem({}, ok, 40), "the edit changed nothing")
        self.assertIn("also changes b.py", autofix.scope_problem({"a.py": 1, "b.py": 1}, ok, 40))
        self.assertIn("binary", autofix.scope_problem({"a.py": None}, ok, 40))
        self.assertIn("41 lines", autofix.scope_problem({"a.py": 41}, ok, 40))
        self.assertEqual(autofix.safe_files(["a.py", "/etc/x", "../y", "b/../../z"]), ["a.py"])

    def test_push_is_plain_and_never_to_the_base(self):
        self.assertEqual(autofix.push_argv("feature", "main"), ["git", "push", "origin", "feature"])
        for branch, base in (("main", "develop"), ("develop", "develop"), ("master", "main"),
                             ("", "main"), ("--force", "main")):
            with self.assertRaises(ValueError):
                autofix.push_argv(branch, base)

    def test_commit_message_and_reply(self):
        j = self.job()
        msg = autofix.commit_message(j, "renamed `t` to `total`")
        self.assertTrue(msg.startswith("Address plaxdan's nit: renamed `t` to `total`\n"))
        self.assertIn(autofix.CO_AUTHOR, msg)
        reply = autofix.reply_text(j, "renamed `t` to `total`.", C, "acme")
        self.assertEqual(reply, f"Done — renamed `t` to `total` in [ccccccc]"
                                f"(https://github.com/acme/api/commit/{C}).\n\nDrafted with Claude 🤖\n")

    def test_rerequest_plan(self):
        answered = [thread("T1", comment(11, REV), comment(99, ME, "Done"), resolved=True)]
        go = lambda s, jobs=(): autofix.rerequest_plan(s, REV, ME, list(jobs), "me-job")  # noqa: E731
        self.assertEqual(go(snap(threads=answered))[0], True)
        approved = [{"id": 2, "state": "APPROVED", "submitted_at": "2026-11-06T11:00:00Z",
                     "author": REV, "is_bot": False}]
        self.assertEqual(go(snap(threads=answered, reviews=approved)),
                         (False, "their latest review is an approval"))
        open_one = answered + [thread("T2", comment(12, REV))]
        self.assertEqual(go(snap(threads=open_one)), (False, "1 of their threads still wait on you"))
        sibling = {**self.job(), "id": "sib", "status": "running"}
        self.assertEqual(go(snap(threads=answered), [sibling])[1],
                         "another of their comments is still being fixed")
        asked = [{"id": "x", "at": "2026-11-06T12:00:00Z", "by": ME, "reviewer": REV}]
        self.assertEqual(go(snap(threads=answered, requests=asked))[1],
                         "their review is already requested")

    def test_template_fills_only_known_placeholders(self):
        out = autofix.fill_template('{path}:{line} {"summary": "{x}"} {plan}',
                                    {"path": "a.py", "line": "3", "plan": "do it"})
        self.assertEqual(out, 'a.py:3 {"summary": "{x}"} do it')
        bundled = autofix.PROMPT_FILE.read_text()
        for k in ("{me}", "{org}", "{pr_url}", "{comment}", "{path}", "{line}", "{plan}"):
            self.assertIn(k, bundled)


class UnpushedTest(Base):
    def test_a_local_branch_ahead_of_origin_is_reported_once_per_clone(self):
        for d in ("api", "api-ABC-1"):
            (self.tmp / "ws" / d / ".git").mkdir(parents=True)

        def run(argv, cwd=None, **kw):
            if "--git-common-dir" in argv:
                return proc(argv, out=f"{self.tmp}/ws/api/.git\n")
            if argv[1] == "rev-parse":
                return proc(argv)
            return proc(argv, out="2\n")
        got = autofix.unpushed_checkouts("api", "feature", self.cfg, run)
        self.assertEqual(got, [f"{self.tmp / 'ws' / 'api'} (2 unpushed)"])

        def none_ahead(argv, cwd=None, **kw):
            return run(argv, cwd) if argv[1] == "rev-parse" else proc(argv, out="0\n")
        self.assertEqual(autofix.unpushed_checkouts("api", "feature", self.cfg, none_ahead), [])


class PipelineTest(Base):
    def test_the_whole_fix(self):
        fake = Fake()
        rc, job = self.run_fix(fake)
        self.assertEqual((rc, job["status"]), (0, "done"))
        self.assertEqual([s["step"] for s in job["steps"]],
                         ["preconditions", "worktree", "assess", "edit", "scope", "validate",
                          "scope-after-validate", "commit", "merge", "push", "reply", "resolve",
                          "rerequest"])
        self.assertTrue(all(s["ok"] for s in job["steps"]))
        self.assertEqual(fake.remote_calls[0], ["git", "push", "origin", "feature"])
        reply = fake.remote_calls[1]
        self.assertIn("repos/acme/api/pulls/7/comments/11/replies", reply)
        self.assertIn("threadId=T1", fake.remote_calls[2])
        self.assertIn("reviewers[]=plaxdan", fake.remote_calls[3])
        self.assertEqual(len(fake.remote_calls), 4)
        self.assertEqual((job["commit_sha"], job["pushed_sha"], job["pushed_from"]), (C, C, H))
        self.assertTrue(job["reply_text"].startswith("Done — renamed `t` to `total` in [ccccccc]"))
        self.assertTrue(job["resolved"] and job["rerequested"])
        commit = next(c for c in fake.calls if c[:2] == ["git", "commit"])
        self.assertIn("Address plaxdan's nit: renamed `t` to `total`", commit[-1])
        banner = next(n for n in fake.notes if n[0].endswith("terminal-notifier"))
        self.assertIn("✅ Fixed for you", banner)
        self.assertTrue(any(n[0] == "/usr/bin/afplay" and n[1].endswith("Purr.aiff")
                            for n in fake.notes))
        self.assertEqual(autofix.load_job(job["id"])["status"], "done")

    def test_the_tools_given_to_claude(self):
        fake = Fake()
        self.run_fix(fake)
        assess, edit = [c for c in fake.calls if c[0] == "claude"]
        allowed = lambda argv: argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]  # noqa: E731
        self.assertEqual(allowed(assess), ["Read", "Grep", "Glob"])
        self.assertIn("Bash", assess[assess.index("--disallowedTools"):])
        wt = str((self.tmp / "worktrees" / "api-7").resolve())
        self.assertEqual(allowed(edit)[3:], [f"Edit(/{wt}/**)", f"Write(/{wt}/**)"])
        self.assertIn("Bash", edit[edit.index("--disallowedTools"):])
        self.assertNotIn("Bash", allowed(edit))
        self.assertIn("Rename t to total.", fake.prompts[1])
        self.assertIn("**Nit:** rename `t` to `total`", fake.prompts[0])

    def test_dry_run_stops_before_the_commit_and_sends_nothing(self):
        fake = Fake()
        rc, job = self.run_fix(fake, dry_run=True)
        self.assertEqual((rc, job["status"]), (0, "dry_run"))
        self.assertNotIn("commit", fake.git_subs())
        self.assertEqual(fake.remote_calls, [])
        self.assertEqual(fake.notes, [])
        self.assertIn("Address plaxdan's nit", job["would_commit"])

    def test_escalations_stop_where_they_happen_and_alert_in_red(self):
        cases = [
            (Fake(assessment={"mechanical": True, "agrees": False, "confidence": 0.9,
                              "files": [], "plan": "", "reason": "the name is fine"}),
             "assess", "Claude disagrees with the comment: the name is fine"),
            (Fake(numstat="1\t1\tsrc/total.py\n2\t0\tsrc/other.py\n"), "scope",
             "the fix reaches beyond the comment: it also changes src/other.py"),
            (Fake(validate_rc=2), "validate", "validation failed (exit 2): error: test failed"),
            (Fake(behind=True, merge_rc=1), "merge", "merging main into the branch conflicts"),
            (Fake(remote_head="f" * 40), "push", "the branch moved on GitHub during the fix"),
            (Fake(edit={"changed": False, "reason": "it needs a design call"}), "edit",
             "Claude didn't make the change: it needs a design call"),
        ]
        for fake, step, reason in cases:
            with self.subTest(step=step):
                rc, job = self.run_fix(fake)
                self.assertEqual((rc, job["status"], job["stopped_at"], job["reason"]),
                                 (1, "escalated", step, reason))
                self.assertEqual([c for c in fake.remote_calls if c[:2] == ["git", "push"]], [])
                if step in ("assess", "edit", "scope", "validate"):
                    self.assertNotIn("commit", fake.git_subs())
                self.assertTrue(any("Couldn't auto-fix" in " ".join(n) for n in fake.notes))
                if step == "merge":
                    self.assertIn(["git", "merge", "--abort"], fake.calls)
                if step == "validate":
                    self.assertIn("error: test failed", job["validate_tail"])

    def test_a_merge_validates_again_before_the_push(self):
        fake = Fake(behind=True)
        _, job = self.run_fix(fake)
        steps = [s["step"] for s in job["steps"]]
        self.assertLess(steps.index("validate-after-merge"), steps.index("push"))
        self.assertEqual(job["pushed_sha"], M)
        self.assertEqual(job["commit_sha"], C)

    def test_no_validate_command_escalates_before_claude_runs(self):
        self.cfg["autofix"]["validate"] = {}
        fake = Fake()
        rc, job = self.run_fix(fake)
        self.assertEqual((rc, job["stopped_at"]), (1, "preconditions"))
        self.assertFalse(any(c[0] == "claude" for c in fake.calls))

    def test_a_reply_that_fails_after_the_push_escalates(self):
        fake = Fake(reply_rc=1)
        rc, job = self.run_fix(fake)
        self.assertEqual((rc, job["stopped_at"]), (1, "reply"))
        self.assertTrue(job["reason"].startswith("pushed ccccccc, but the reply didn't post"))
        self.assertEqual(job["pushed_sha"], C)

    def test_answered_yourself_clears_quietly(self):
        fake = Fake()
        answered = snap(threads=[thread("T1", comment(11, REV), comment(20, ME, "on it"))])
        rc, job = self.run_fix(fake, snaps=[answered])
        self.assertEqual((rc, job["status"], job["cleared"]), (0, "cleared", "you replied in the thread"))
        self.assertEqual(fake.notes, [])

    def test_a_dry_run_job_that_stops_sends_no_alert(self):
        fake = Fake(validate_rc=1)
        rc, job = self.run_fix(fake, dry_run=True)
        self.assertEqual((rc, job["status"], job["dry_run"]), (1, "escalated", True))
        self.assertEqual(fake.notes, [])

    def test_another_fix_holding_the_pr_lock_escalates(self):
        import fcntl
        autofix.AUTOFIX_DIR.mkdir(parents=True, exist_ok=True)
        with (autofix.AUTOFIX_DIR / "api-7.lock").open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fake = Fake()
            rc, job = self.run_fix(fake)
        self.assertEqual((rc, job["reason"]), (1, "another auto-fix is running on this PR"))
        self.assertEqual(fake.calls, [])


class CommandTest(Base):
    def test_refuses_without_dry_run_while_off(self):
        self.cfg["autofix"]["enabled"] = False
        out = []
        self.assertEqual(autofix.run_command("abc", self.cfg, False, out=out.append), 2)
        self.assertIn("enabled is off", out[0])

    def test_dry_run_from_a_comment_link_writes_nothing(self):
        self.cfg["autofix"]["enabled"] = False
        fake = Fake()
        out = []
        rc = autofix.run_command(f"{URL}#discussion_r11", self.cfg, True,
                                 tools=self.tools(fake, snaps=[snap()]), out=out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(autofix.all_jobs(), [])
        self.assertEqual(fake.remote_calls, [])
        self.assertNotIn("commit", fake.git_subs())
        text = "\n".join(out)
        self.assertIn("Would commit:", text)
        self.assertIn("git push origin feature", text)
        self.assertIn("Stopped: dry_run", text)


class StartQueuedTest(Base):
    def test_one_fix_per_pr_and_nothing_while_off(self):
        a = autofix.new_job(snap(), opened("T1", 11), self.cfg)
        b = autofix.new_job(snap(), opened("T2", 12), self.cfg)
        other = autofix.new_job({**snap(), "id": "PR2", "number": 8}, opened("T3", 13), self.cfg)
        for j in (a, b, other):
            autofix.save_job(j)
        spawned = []
        spawn = lambda jid: spawned.append(jid) or 4242  # noqa: E731
        self.cfg["autofix"]["enabled"] = False
        self.assertEqual(autofix.start_queued(self.cfg, spawn, lambda: True), (0, False))
        self.cfg["autofix"]["enabled"] = True
        with unittest.mock.patch.object(autofix.followup, "_alive", return_value=True):
            self.assertEqual(autofix.start_queued(self.cfg, spawn, lambda: True), (2, False))
            self.assertEqual(len(spawned), 2)
            self.assertEqual({autofix.load_job(j)["pr"]["id"] for j in spawned}, {"PR1", "PR2"})
            self.assertEqual(autofix.start_queued(self.cfg, spawn, lambda: True), (0, False))

    def test_a_dead_run_escalates(self):
        j = {**autofix.new_job(snap(), opened(), self.cfg), "status": "running", "pid": 1}
        autofix.save_job(j)
        with unittest.mock.patch.object(autofix.followup, "_alive", return_value=False):
            autofix.start_queued(self.cfg, lambda jid: 1, lambda: True)
        got = autofix.load_job(j["id"])
        self.assertEqual((got["status"], got["reason"]), ("escalated", "the auto-fix process died"))


if __name__ == "__main__":
    unittest.main()
