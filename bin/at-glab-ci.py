#!/usr/bin/env python3
"""Control GitLab project pipelines via REST API (GitLab CE/EE 17.x compatible)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_GITLAB_URL = "https://glab.espressif.cn"
DEFAULT_PROJECT_ID = "2026"
DEFAULT_TOKEN_FILE = "~/.glab_auth_token"
API_VERSION = "v4"

# Statuses that GitLab allows to cancel (pipelines still "in progress")
CANCELABLE_STATUSES = frozenset(
    {
        "created",
        "waiting_for_resource",
        "preparing",
        "pending",
        "running",
    }
)


def load_token() -> str:
    env = os.environ.get("GITLAB_TOKEN", "").strip()
    if env:
        return env
    path = os.path.expanduser(DEFAULT_TOKEN_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        raise SystemExit(
            "No GitLab token: set GITLAB_TOKEN or create "
            f"{DEFAULT_TOKEN_FILE} (PAT with api scope)."
        )
    except OSError as e:
        raise SystemExit(f"Cannot read {path}: {e}") from e
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise SystemExit(f"{path} has no non-empty token line.")


def api_request(
    method: str,
    base_url: str,
    project_id: str,
    token: str,
    path: str,
    *,
    query: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    q = ""
    if query:
        q = "?" + urllib.parse.urlencode(query)
    url = f"{base_url.rstrip('/')}/api/{API_VERSION}/projects/{urllib.parse.quote(project_id, safe='')}{path}{q}"
    data = None
    headers = {"PRIVATE-TOKEN": token}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            code = resp.getcode()
            raw = resp.read().decode("utf-8")
            if not raw:
                return code, None
            return code, json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
        except json.JSONDecodeError:
            parsed = err_body
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {parsed}") from e


def paginate_pipelines(
    base_url: str,
    project_id: str,
    token: str,
    *,
    extra_query: dict[str, str] | None = None,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while page <= max_pages:
        q: dict[str, str] = {"per_page": str(per_page), "page": str(page)}
        if extra_query:
            q.update(extra_query)
        _, data = api_request("GET", base_url, project_id, token, "/pipelines", query=q)
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected pipelines response: {data!r}")
        if not data:
            break
        out.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return out


def cmd_stop_all(base_url: str, project_id: str, token: str) -> int:
    """Cancel every pipeline that is still cancelable (dedupe by id)."""
    seen: set[int] = set()
    to_cancel: list[dict[str, Any]] = []
    for scope in ("running", "pending"):
        for p in paginate_pipelines(
            base_url, project_id, token, extra_query={"scope": scope}
        ):
            pid = int(p["id"])
            if pid in seen:
                continue
            seen.add(pid)
            st = p.get("status") or ""
            if st in CANCELABLE_STATUSES:
                to_cancel.append(p)

    if not to_cancel:
        print("No running/pending pipelines to cancel.")
        return 0

    ok = 0
    for p in to_cancel:
        pid = int(p["id"])
        try:
            _, _ = api_request(
                "POST",
                base_url,
                project_id,
                token,
                f"/pipelines/{pid}/cancel",
            )
            print(f"Canceled pipeline {pid} (ref={p.get('ref')!r} status={p.get('status')})")
            ok += 1
        except RuntimeError as e:
            print(f"Skip pipeline {pid}: {e}", file=sys.stderr)
    print(f"Done: canceled {ok}/{len(to_cancel)} pipeline(s).")
    return 0 if ok == len(to_cancel) else 1


def get_default_branch(base_url: str, project_id: str, token: str) -> str:
    _, proj = api_request("GET", base_url, project_id, token, "")
    if not isinstance(proj, dict) or "default_branch" not in proj:
        raise RuntimeError(f"Could not read default_branch from project: {proj!r}")
    return str(proj["default_branch"])


def cmd_run(base_url: str, project_id: str, token: str, ref: str | None) -> int:
    branch = ref or get_default_branch(base_url, project_id, token)
    _, pipe = api_request(
        "POST",
        base_url,
        project_id,
        token,
        "/pipeline",
        body={"ref": branch},
    )
    if not isinstance(pipe, dict):
        raise SystemExit(f"Unexpected response: {pipe!r}")
    print(
        f"Started pipeline id={pipe.get('id')} ref={pipe.get('ref')!r} "
        f"status={pipe.get('status')} web_url={pipe.get('web_url')}"
    )
    return 0


def cmd_retry(
    base_url: str,
    project_id: str,
    token: str,
    *,
    pipeline_id: int | None,
    all_failed: bool,
) -> int:
    if pipeline_id is not None:
        targets = [pipeline_id]
    elif all_failed:
        failed = paginate_pipelines(
            base_url, project_id, token, extra_query={"status": "failed"}
        )
        targets = [int(p["id"]) for p in failed]
        if not targets:
            print("No failed pipelines found.")
            return 0
    else:
        _, first = api_request(
            "GET",
            base_url,
            project_id,
            token,
            "/pipelines",
            query={
                "status": "failed",
                "per_page": "1",
                "page": "1",
                "order_by": "id",
                "sort": "desc",
            },
        )
        if not isinstance(first, list) or not first:
            print("No failed pipeline to retry.")
            return 0
        targets = [int(first[0]["id"])]

    errors = 0
    for pid in targets:
        try:
            _, pipe = api_request(
                "POST",
                base_url,
                project_id,
                token,
                f"/pipelines/{pid}/retry",
            )
            if isinstance(pipe, dict):
                print(f"Retried pipeline {pid} -> status={pipe.get('status')} web_url={pipe.get('web_url')}")
            else:
                print(f"Retried pipeline {pid}")
        except RuntimeError as e:
            print(f"Pipeline {pid}: {e}", file=sys.stderr)
            errors += 1
    if errors:
        print(f"Finished with {errors} error(s) on {len(targets)} pipeline(s).", file=sys.stderr)
        return 1
    print(f"Done: retried {len(targets)} pipeline(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="at-glab-ci.py",
        description="Control GitLab project pipelines via REST API v4.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
environment:
  GITLAB_URL         Base URL (no trailing slash); overridden by -u/--url
  GITLAB_TOKEN       PAT with api scope; else read from ~/.glab_auth_token
  GITLAB_PROJECT_ID  Numeric project id; overridden by -p/--project-id

token file (when GITLAB_TOKEN unset):
  ~/.glab_auth_token   One line: the token. Lines starting with # are ignored.

examples:
  %(prog)s stop-all
  %(prog)s run
  %(prog)s run -r master
  %(prog)s retry
  %(prog)s retry -i 12345
  %(prog)s retry -a
  %(prog)s -u https://gitlab.example.com -p 42 run -r develop
""",
    )
    p.add_argument(
        "-u",
        "--url",
        default=os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL),
        metavar="URL",
        help=f"GitLab base URL (default: env GITLAB_URL or {DEFAULT_GITLAB_URL})",
    )
    p.add_argument(
        "-p",
        "--project-id",
        default=os.environ.get("GITLAB_PROJECT_ID", DEFAULT_PROJECT_ID),
        metavar="ID",
        help=f"Project id (default: env GITLAB_PROJECT_ID or {DEFAULT_PROJECT_ID})",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser(
        "stop-all",
        help="Cancel all active pipelines (running/pending scope)",
        description="Cancel every pipeline still in a cancelable state "
        "(created, waiting_for_resource, preparing, pending, running).",
    )

    pr = sub.add_parser(
        "run",
        help="Create a new pipeline",
        description="Create a new pipeline on the given ref "
        "(or the project default branch if -r/--ref is omitted).",
    )
    pr.add_argument(
        "-r",
        "--ref",
        default=None,
        metavar="REF",
        help="Branch or tag (default: project default_branch)",
    )

    rr = sub.add_parser(
        "retry",
        help="Retry failed pipeline(s)",
        description="Retry failed pipeline(s): latest failed by default, "
        "a specific id with -i/--id, or all failed with -a/--all-failed.",
    )
    g = rr.add_mutually_exclusive_group()
    g.add_argument(
        "-i",
        "--id",
        type=int,
        default=None,
        metavar="PIPELINE_ID",
        help="Retry this pipeline id only",
    )
    g.add_argument(
        "-a",
        "--all-failed",
        action="store_true",
        help="Retry all failed pipelines (paginated, can be many)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(argv)
    try:
        token = load_token()
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        print("Create a token with at least 'api' scope in GitLab user settings.", file=sys.stderr)
        return 2

    base_url = args.url
    project_id = str(args.project_id)

    if args.command == "stop-all":
        return cmd_stop_all(base_url, project_id, token)
    if args.command == "run":
        return cmd_run(base_url, project_id, token, args.ref)
    if args.command == "retry":
        return cmd_retry(
            base_url,
            project_id,
            token,
            pipeline_id=args.id,
            all_failed=args.all_failed,
        )
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
