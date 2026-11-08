"""Runner orchestration with a fake agent; no model calls or containers."""

import argparse
from concurrent.futures import Future
import importlib
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest import mock

import pandas as pd

with tempfile.TemporaryDirectory() as _config_dir:
    _config_path = Path(_config_dir) / "config.json"
    _config_path.write_text(json.dumps({"gemini3pro": {"api_key": "test-only", "model": "fake-model"}}))
    with mock.patch.dict(os.environ, {"API_CONFIG_PATH": str(_config_path)}):
        router = importlib.reload(importlib.import_module("rca.api_router"))
        runner = importlib.reload(importlib.import_module("rca.run_agent_standard"))
import rca.baseline.rca_agent.rca_agent as rca_agent_module


class FakeAgent:
    def __init__(self, ap, bp):
        pass

    def run(self, instruction, logger, max_step, max_turn):
        if "timeout" in instruction:
            time.sleep(5)
        if "swallow" in instruction:
            # Mimics the OpenAI SDK, which retries any Exception raised during a
            # request: a TimeoutError alarm would be absorbed here.
            for _ in range(3):
                try:
                    time.sleep(5)
                except Exception:
                    continue
        if "boom" in instruction:
            raise RuntimeError("agent exploded")
        return (f"pred for {instruction}",
                [{"code": "print(1)", "result": "1"}],
                [{"role": "user", "content": instruction}])


class SyncExecutor:
    """Stands in for ProcessPoolExecutor: runs jobs in-process, finishing in reverse order."""

    def __init__(self, *args, **kwargs):
        self.futures = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        self.futures.append(future)
        return future


class CrashAwareExecutor(SyncExecutor):
    """Surfaces an exception from future.result(), as a dead worker process would."""

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as e:
            future.set_exception(e)
        return future


def _reverse_completed(futures):
    return list(reversed(list(futures)))


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        Path("dataset/Bank").mkdir(parents=True)
        self.queries = ["ok zero", "timeout one", "boom two", "ok three"]
        pd.DataFrame({
            "task_index": [f"task_{i + 1}" for i in range(4)],
            "instruction": self.queries,
            "scoring_points": ["sp"] * 4,
        }).to_csv("dataset/Bank/query.csv", index=False)
        pd.DataFrame({"component": list("abcd"), "reason": list("wxyz")}).to_csv(
            "dataset/Bank/record.csv", index=False)
        patcher = mock.patch.object(rca_agent_module, "RCA_Agent", FakeAgent)
        patcher.start()
        self.addCleanup(patcher.stop)
        eval_patch = mock.patch.object(runner, "evaluate", return_value=(["c1"], [], 1.0))
        self.evaluate = eval_patch.start()
        self.addCleanup(eval_patch.stop)

    def args(self, **overrides):
        values = dict(tag="t", timeout=1, controller_max_step=1, controller_max_turn=1,
                      no_score=False, sample_num=1, start_idx=0, end_idx=10, workers=1)
        values.update(overrides)
        return argparse.Namespace(**values)

    def eval_file(self):
        return Path("test/result/Bank/agent-t-fake-model.csv")

    def test_sequential_skips_timeout_and_error_and_keeps_going(self):
        runner.main(self.args(), "uid", "Bank")
        df = pd.read_csv(self.eval_file())
        self.assertEqual(df.row_id.tolist(), [0, 3])
        self.assertEqual(df.prediction.tolist(), ["pred for ok zero", "pred for ok three"])
        self.assertEqual(df.score.tolist(), [1.0, 1.0])
        self.assertEqual(df.groundtruth[0], "component: a\nreason: w")
        # The SIGALRM must not stay armed after the timed-out or crashed sample.
        self.assertEqual(signal.alarm(0), 0)
        notebooks = sorted(p.name for p in Path("test/monitor/Bank/agent-t-fake-model/uid/trajectory").iterdir())
        self.assertEqual(notebooks, ["uid_#0-0.ipynb", "uid_#3-0.ipynb"])
        logs = Path("test/monitor/Bank/agent-t-fake-model/uid/history")
        self.assertIn("exceeded the time limit", (logs / "uid_#1-0.log").read_text())
        self.assertIn("agent exploded", (logs / "uid_#2-0.log").read_text())

    def test_timeout_is_not_swallowed_by_retrying_code(self):
        pd.DataFrame({"task_index": ["task_1"], "instruction": ["swallow zero"],
                      "scoring_points": ["sp"]}).to_csv("dataset/Bank/query.csv", index=False)
        started = time.monotonic()
        runner.main(self.args(), "uid", "Bank")
        self.assertLess(time.monotonic() - started, 4, "the alarm was absorbed by an except Exception")
        self.assertFalse(self.eval_file().exists())
        self.assertEqual(signal.alarm(0), 0)

    def test_parallel_results_written_in_query_order(self):
        with mock.patch.object(runner, "ProcessPoolExecutor", SyncExecutor), \
             mock.patch.object(runner, "as_completed", _reverse_completed):
            runner.main(self.args(workers=4), "uid", "Bank")
        df = pd.read_csv(self.eval_file())
        self.assertEqual(df.row_id.tolist(), [0, 3])

    def test_parallel_worker_crash_is_recorded_not_raised(self):
        original_run_sample = runner.run_sample

        def crashing(args, uid, dataset, job):
            if job["row_id"] == 0:
                raise OSError("worker died")
            return original_run_sample(args, uid, dataset, job)

        with mock.patch.object(runner, "ProcessPoolExecutor", CrashAwareExecutor), \
             mock.patch.object(runner, "run_sample", crashing):
            runner.main(self.args(workers=2), "uid", "Bank")
        self.assertEqual(pd.read_csv(self.eval_file()).row_id.tolist(), [3])

    def test_existing_rows_are_kept_and_new_rows_appended(self):
        runner.main(self.args(start_idx=0, end_idx=0), "uid1", "Bank")
        runner.main(self.args(start_idx=3, end_idx=3), "uid2", "Bank")
        self.assertEqual(pd.read_csv(self.eval_file()).row_id.tolist(), [0, 3])

    def test_no_score_skips_evaluate(self):
        runner.main(self.args(no_score=True, end_idx=0), "uid", "Bank")
        self.evaluate.assert_not_called()
        df = pd.read_csv(self.eval_file(), keep_default_na=False)
        self.assertEqual(df.score.tolist(), ["N/A"])

    def test_scoring_failure_keeps_unscored_prediction(self):
        self.evaluate.side_effect = ZeroDivisionError("no rubric")
        runner.main(self.args(end_idx=0), "uid", "Bank")
        df = pd.read_csv(self.eval_file(), keep_default_na=False)
        self.assertEqual(df.prediction.tolist(), ["pred for ok zero"])
        self.assertEqual(df.score.tolist(), ["N/A"])

    def test_sample_num_runs_each_query_repeatedly(self):
        runner.main(self.args(sample_num=2, end_idx=0), "uid", "Bank")
        self.assertEqual(len(pd.read_csv(self.eval_file())), 2)

    def test_catalog(self):
        self.assertEqual([runner._catalog(f"task_{i}") for i in (1, 4, 7, 8, 40)],
                         ["easy", "middle", "hard", "other", "other"])


if __name__ == "__main__":
    unittest.main()
