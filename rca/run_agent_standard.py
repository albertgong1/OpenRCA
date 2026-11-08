import os
import importlib
import sys
import json
import argparse
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)
from main.evaluate import evaluate
from rca.api_router import configs

from datetime import datetime
from loguru import logger
from nbformat import v4 as nbf
import pandas as pd
import signal

_PROMPT_MODULES = {
    "Telecom": "basic_prompt_Telecom",
    "Bank": "basic_prompt_Bank",
    "Market/cloudbed-1": "basic_prompt_Market",
    "Market/cloudbed-2": "basic_prompt_Market",
    "OtelDemo": "basic_prompt_OtelDemo",
}

# Default loguru format plus the sample being run, so interleaved output from
# parallel workers stays attributable. Per-sample log files keep the default.
_STDOUT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<magenta>{extra[sample]}</magenta> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)

_EVAL_COLUMNS = ["row_id", "task_index", "instruction", "prediction", "groundtruth", "passed", "failed", "score"]


class SampleTimeout(BaseException):
    """Raised from SIGALRM when a sample exceeds --timeout.

    Deliberately not a TimeoutError (or any Exception). On Python >= 3.10
    TimeoutError is socket.timeout, so an alarm landing during a model API call,
    where a sample spends most of its time, was mapped by httpcore to
    httpx.ReadTimeout and silently retried by the OpenAI SDK; the one-shot alarm
    was then gone and the sample ran with no limit. A BaseException is not
    retried by the SDK and passes through the agent's `except Exception`
    handlers; the sandbox still closes its container on the way out.
    """


def handler(signum, frame):
    raise SampleTimeout("Loop execution exceeded the time limit")


def _load_basic_prompt(dataset):
    if dataset not in _PROMPT_MODULES:
        # The if/elif this replaces had no else, so an unknown dataset left bp
        # unbound and failed later with an opaque NameError.
        raise ValueError(
            f"No basic prompt for dataset {dataset!r}. "
            f"Known datasets: {', '.join(sorted(_PROMPT_MODULES))}. "
            "Add rca/baseline/rca_agent/prompt/basic_prompt_<name>.py "
            "and register it in _PROMPT_MODULES."
        )
    return importlib.import_module(
        f"rca.baseline.rca_agent.prompt.{_PROMPT_MODULES[dataset]}"
    )


def _paths(args, uid, dataset):
    model = configs['MODEL'].split('/')[-1]
    eval_file = f"test/result/{dataset}/agent-{args.tag}-{model}.csv"
    obs_path = f"test/monitor/{dataset}/agent-{args.tag}-{model}"
    return eval_file, f"{obs_path}/{uid}"


def _catalog(task_index):
    task_id = int(task_index.split('_')[1])
    if task_id <= 3:
        return "easy"
    if task_id <= 6:
        return "middle"
    if task_id <= 7:
        return "hard"
    # Task ids above 7 (e.g. OtelDemo's task_1..task_40) are not difficulty levels.
    return "other"


def _setup_stdout_logging():
    logger.remove()
    logger.configure(extra={"sample": "main"})
    logger.add(sys.stdout, colorize=True, enqueue=True, level="INFO", format=_STDOUT_FORMAT)


def run_sample(args, uid, dataset, job):
    """Run one attempt at one query and return (eval_row or None, status).

    Runs in a worker process when --workers > 1, otherwise in the main process.
    Each call creates its own agent and sandbox container, sets its own SIGALRM
    timeout (each worker process has its own main thread) and writes its own
    trajectory, prompt and log files. Only the main process writes the eval CSV.

    A timeout or any other exception skips this sample instead of aborting the
    run, so one failing query cannot take down the queries running beside it.
    """
    from rca.baseline.rca_agent.rca_agent import RCA_Agent
    import rca.baseline.rca_agent.prompt.agent_prompt as ap
    bp = _load_basic_prompt(dataset)

    idx, i = job["row_id"], job["sample"]
    _, unique_obs_path = _paths(args, uid, dataset)
    uuid = uid + f"_#{idx}-{i}"
    nb = nbf.new_notebook()
    nbfile = f"{unique_obs_path}/trajectory/{uuid}.ipynb"
    promptfile = f"{unique_obs_path}/prompt/{uuid}.json"
    logfile = f"{unique_obs_path}/history/{uuid}.log"

    _setup_stdout_logging()
    file_sink = logger.add(logfile, colorize=True, enqueue=True, level="INFO")
    log = logger.bind(sample=f"#{idx}-{i}")
    log.debug('\n' + "#"*80 + f"\n{uuid}: {job['task_index']}\n" + "#"*80)
    try:
        signal.signal(signal.SIGALRM, handler)
        try:
            signal.alarm(args.timeout)
            try:
                agent = RCA_Agent(ap, bp)
                prediction, trajectory, prompt = agent.run(job["instruction"],
                                                           log,
                                                           max_step=args.controller_max_step,
                                                           max_turn=args.controller_max_turn)
            finally:
                # Previously the alarm stayed armed if agent.run raised, so it
                # could fire later inside an unrelated query.
                signal.alarm(0)
        except SampleTimeout:
            log.error(f"Loop {i} exceeded the time limit and was skipped")
            return None, "timeout"
        except Exception as e:
            log.opt(exception=True).error(f"Loop {i} failed and was skipped: {type(e).__name__}: {e}")
            return None, f"error: {type(e).__name__}: {e}"

        for step in trajectory:
            code_cell = nbf.new_code_cell(step['code'])
            result_cell = nbf.new_markdown_cell(f"```\n{step['result']}\n```")
            nb.cells.append(code_cell)
            nb.cells.append(result_cell)
        with open(nbfile, 'w', encoding='utf-8') as f:
            json.dump(nb, f, ensure_ascii=False, indent=4)
        log.info(f"Trajectory has been saved to {nbfile}")

        with open(promptfile, 'w', encoding='utf-8') as f:
            json.dump({"messages": prompt}, f, ensure_ascii=False, indent=4)
        log.info(f"Prompt has been saved to {promptfile}")

        eval_row = {"row_id": idx,
                    "task_index": job["task_index"],
                    "instruction": job["instruction"],
                    "prediction": prediction,
                    "groundtruth": job["groundtruth"],
                    "passed": "N/A",
                    "failed": "N/A",
                    "score": "N/A"}

        if args.no_score:
            # Scoring happens out of band (e.g. against a different judge), so
            # skip evaluate() entirely and leave passed/failed/score as "N/A"
            # for the external scorer to fill in. This also means query.csv
            # needs no scoring_points column: a rubric that matches none of
            # evaluate()'s three patterns gives it a zero denominator and a
            # ZeroDivisionError.
            log.info(f"Prediction: {prediction}")
            log.info("Scoring skipped (--no-score)")
            return eval_row, "ok"

        try:
            passed_criteria, failed_criteria, score = evaluate(prediction, job["scoring_points"])
        except Exception as e:
            # Keep the prediction rather than losing a finished diagnosis.
            log.opt(exception=True).error(f"Scoring failed, prediction kept unscored: {type(e).__name__}: {e}")
            return eval_row, "unscored"

        log.info(f"Prediction: {prediction}")
        log.info(f"Scoring Points: {job['scoring_points']}")
        log.info(f"Passed Criteria: {passed_criteria}")
        log.info(f"Failed Criteria: {failed_criteria}")
        log.info(f"Score: {score}")
        eval_row.update(passed='\n'.join(passed_criteria),
                        failed='\n'.join(failed_criteria),
                        score=score)
        return eval_row, "ok"
    finally:
        # Drain the enqueued file sink so the log is complete before this
        # process moves on to its next sample.
        logger.remove(file_sink)


def _summarize(dataset, jobs, outcomes, scored):
    counts = {}
    for status in outcomes.values():
        key = status.split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    logger.info(f"[{dataset}] Finished {len(jobs)} samples: "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    skipped = sorted(k for k, s in outcomes.items() if s not in ("ok", "unscored"))
    if skipped:
        logger.warning(f"[{dataset}] Skipped samples (row_id, sample), rerun with "
                       f"--start_idx/--end_idx: {skipped}")
    if scored:
        # Best score per query across its samples, as before.
        best = {}
        for (idx, _), (catalog, score) in scored.items():
            best[idx] = (catalog, max(score, best.get(idx, (catalog, 0))[1]))
        by_catalog = {}
        for catalog, score in best.values():
            by_catalog.setdefault(catalog, []).append(score)
        total = [s for _, s in best.values()]
        logger.info(f"[{dataset}] Mean best score over {len(total)} scored queries: "
                    f"{sum(total) / len(total):.3f}")
        for catalog in sorted(by_catalog):
            s = by_catalog[catalog]
            logger.info(f"[{dataset}]   {catalog}: {sum(s) / len(s):.3f} over {len(s)} queries")


def main(args, uid, dataset):
    _load_basic_prompt(dataset)  # fail fast on an unknown dataset

    inst_file = f"dataset/{dataset}/query.csv"
    gt_file = f"dataset/{dataset}/record.csv"
    if not os.path.exists(inst_file) or not os.path.exists(gt_file):
        raise FileNotFoundError(f"Please download the dataset first.")
    instruct_data = pd.read_csv(inst_file)
    gt_data = pd.read_csv(gt_file)

    eval_file, unique_obs_path = _paths(args, uid, dataset)
    for sub in ("history", "trajectory", "prompt"):
        os.makedirs(f"{unique_obs_path}/{sub}", exist_ok=True)
    os.makedirs(os.path.dirname(eval_file), exist_ok=True)
    if os.path.exists(eval_file):
        existing_df = pd.read_csv(eval_file)
    else:
        existing_df = pd.DataFrame(columns=_EVAL_COLUMNS)

    jobs = []
    for idx, row in instruct_data.iterrows():
        if idx < args.start_idx:
            continue
        if idx > args.end_idx:
            break
        groundtruth = '\n'.join([f'{col}: {gt_data.iloc[idx][col]}' for col in gt_data.columns if col != 'description'])
        for i in range(args.sample_num):
            jobs.append({"row_id": idx,
                         "sample": i,
                         "task_index": row["task_index"],
                         "instruction": row["instruction"],
                         "scoring_points": row["scoring_points"] if "scoring_points" in row else "",
                         "groundtruth": groundtruth})

    logger.info(f"Using dataset: {dataset}")
    logger.info(f"Using model: {configs['MODEL'].split('/')[-1]}")
    logger.info(f"Running {len(jobs)} samples with {args.workers} worker(s)")

    new_rows = {}   # (row_id, sample) -> eval row
    outcomes = {}   # (row_id, sample) -> status
    scored = {}     # (row_id, sample) -> (catalog, score)

    def record(job, eval_row, status):
        key = (job["row_id"], job["sample"])
        outcomes[key] = status
        if eval_row is None:
            return
        new_rows[key] = eval_row
        if isinstance(eval_row["score"], (int, float)):
            scored[key] = (_catalog(job["task_index"]), eval_row["score"])
        # Rewrite after every finished sample so an interrupted run keeps its
        # results; new rows are kept in query order whatever order they finish in.
        ordered = [new_rows[k] for k in sorted(new_rows)]
        pd.concat([existing_df, pd.DataFrame(ordered, columns=_EVAL_COLUMNS)],
                  ignore_index=True).to_csv(eval_file, index=False)

    if args.workers <= 1:
        for job in jobs:
            eval_row, status = run_sample(args, uid, dataset, job)
            record(job, eval_row, status)
    else:
        # spawn, not fork: workers start clean instead of inheriting the parent's
        # loguru queue threads and locks mid-state.
        with ProcessPoolExecutor(max_workers=args.workers, mp_context=mp.get_context("spawn")) as pool:
            futures = {pool.submit(run_sample, args, uid, dataset, job): job for job in jobs}
            for future in as_completed(futures):
                job = futures[future]
                try:
                    eval_row, status = future.result()
                except Exception as e:  # e.g. a worker process died
                    logger.opt(exception=True).error(
                        f"Sample #{job['row_id']}-{job['sample']} crashed its worker: {type(e).__name__}: {e}")
                    eval_row, status = None, f"error: {type(e).__name__}: {e}"
                record(job, eval_row, status)

    _summarize(dataset, jobs, outcomes, scored)


if __name__ == "__main__":

    uid = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default="Market/cloudbed-1")
    parser.add_argument("--sample_num", type=int, default=1)
    parser.add_argument("--start_idx", type=int, default=0)
    parser.add_argument("--end_idx", type=int, default=150)
    parser.add_argument("--controller_max_step", type=int, default=25)
    parser.add_argument("--controller_max_turn", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--tag", type=str, default='rca')
    parser.add_argument("--auto", type=bool, default=False)
    parser.add_argument("--no-score", action="store_true",
                        help="Run the agent and save predictions without scoring them. "
                             "Use when predictions are scored out of band by another judge.")
    parser.add_argument("--workers", type=int, default=1,
                        help="Number of samples to run in parallel, each in its own process "
                             "and sandbox container. Each sandbox may use up to 2 CPUs and "
                             "8 GiB of memory. Default 1 runs sequentially in-process.")

    args = parser.parse_args()
    _setup_stdout_logging()

    if args.auto:
        print(f"Auto mode is on. Model is fixed to {configs['MODEL']}")
        datasets = ["Market/cloudbed-1", "Market/cloudbed-2", "Bank", "Telecom"]
        for dataset in datasets:
            main(args, uid, dataset)
    else:
        dataset = args.dataset
        main(args, uid, dataset)
