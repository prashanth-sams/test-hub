"""The dashboard's window arithmetic, run for real under node.

``05-windows.js`` turns the URL's ``window`` into API parameters, puts it into
words, and transcribes the server's trend bucketing — a filtered slice is
bucketed in the browser, and its chart has to look like the one the server
draws for everything. These run that file, and the provider's bucketing, under
node and hold them to the Python they transcribe, in more than one time zone.

Skipped where node is not installed; the static guards in
``test_frontend_assets.py`` run everywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from test_hub.api.app import web_root
from test_hub.storage.repositories import SqlRunRepository

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

SCRIPTS = [web_root() / "assets/js/05-windows.js", web_root() / "assets/js/10-provider.js"]

#: Loads the two files the way the page does — classic scripts sharing one
#: global — with the one helper of 00-util.js that the provider's bucketing
#: needs, then runs the test's snippet with the namespaces in hand.
HARNESS = r"""
global.window = global;
window.TestHub = {
  util: {
    toDate: function (value) {
      if (value === null || value === undefined) { return null; }
      var date = value instanceof Date ? value : new Date(value);
      return isNaN(date.getTime()) ? null : date;
    }
  }
};
const fs = require('fs');
const vm = require('vm');
for (const file of JSON.parse(process.argv[2])) {
  vm.runInThisContext(fs.readFileSync(file, 'utf8'), { filename: file });
}
const body = fs.readFileSync(0, 'utf8');
const result = new Function('W', 'P', body)(window.TestHub.windows, window.TestHub.provider);
process.stdout.write(JSON.stringify(result));
"""


def run_js(tmp_path: Path, body: str, *, tz: str = "UTC") -> Any:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    env = {**os.environ, "TZ": tz, "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"}
    result = subprocess.run(
        [str(NODE), str(harness), json.dumps([str(path) for path in SCRIPTS])],
        input=body,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


#: The en dash ranges are written with.
DASH = "\u2013"


def plain(text: str) -> str:
    """ICU puts narrow and no-break spaces in times ("8:00 AM"); compare words."""
    return text.replace("\u202f", " ").replace("\u00a0", " ")


# ------------------------------------------------------------------ presets


def test_every_preset_asks_the_api_for_itself(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        """
        return W.PRESETS.map(function (length) {
          var win = W.parse(length.key);
          return [win.value, win.preset, W.apiParams(win)];
        });
        """,
    )
    assert result == [
        ["1h", True, {"window_hours": 1}],
        ["6h", True, {"window_hours": 6}],
        ["24h", True, {"window_hours": 24}],
        ["7d", True, {"window_days": 7}],
        ["14d", True, {"window_days": 14}],
        ["30d", True, {"window_days": 30}],
        ["90d", True, {"window_days": 90}],
    ]


def test_old_links_and_odd_lengths_still_open(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        """
        return {
          legacy: W.fromQuery({ window_days: '7' }).value,
          legacyOdd: W.fromQuery({ window_days: '60' }).value,
          both: W.fromQuery({ window: '6h', window_days: '7' }).value,
          none: W.fromQuery({}).value,
          odd: [W.parse('3d').value, W.parse('3d').preset, W.apiParams(W.parse('3d'))]
        };
        """,
    )
    assert result == {
        "legacy": "7d",
        "legacyOdd": "60d",
        "both": "6h",
        "none": "30d",
        "odd": ["3d", False, {"window_days": 3}],
    }


@pytest.mark.parametrize(
    "value",
    [
        "",
        "week",
        "0h",
        "366d",
        "8785h",
        "2025-11-12T00:00..",  # no offset: the API would refuse it
        "2025-11-12T00:00Z..2025-11-11T00:00Z",  # ends before it starts
        "2020-01-01T00:00Z..2025-01-01T00:00Z",  # longer than 366 days
        "2999-01-01T00:00Z..",  # starts in the future
    ],
)
def test_anything_the_server_would_refuse_opens_the_default(tmp_path: Path, value: str) -> None:
    assert run_js(tmp_path, f"return W.parse({json.dumps(value)}).value;") == "30d"


# ------------------------------------------------------------------- ranges


def test_a_range_round_trips_through_the_url(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        """
        var win = W.range(new Date('2025-11-10T08:00:00Z'), new Date('2025-11-11T18:30:00Z'));
        var again = W.parse(win.value);
        var since = new Date(Date.now() - 2 * 3600000);
        since.setUTCSeconds(0, 0);
        var open = W.parse(W.range(since, null).value);
        return {
          value: win.value,
          same: again.kind === 'range' && again.value === win.value,
          api: W.apiParams(again),
          open: [open.kind, open.to, Object.keys(W.apiParams(open))]
        };
        """,
    )
    assert result == {
        "value": "2025-11-10T08:00Z..2025-11-11T18:30Z",
        "same": True,
        "api": {"since": "2025-11-10T08:00:00.000Z", "until": "2025-11-11T18:30:00.000Z"},
        "open": ["range", None, ["since"]],
    }


def test_the_picker_says_what_is_wrong_with_a_range(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        """
        var now = Date.parse('2025-11-12T10:00:00Z');
        function at(text) { return new Date(text); }
        return [
          W.rangeProblem(null, null, now),
          W.rangeProblem(at('2025-11-13T00:00:00Z'), null, now),
          W.rangeProblem(at('2025-11-11T00:00:00Z'), at('2025-11-10T00:00:00Z'), now),
          W.rangeProblem(at('2023-01-01T00:00:00Z'), at('2025-01-01T00:00:00Z'), now),
          W.rangeProblem(at('2025-11-10T00:00:00Z'), at('2025-11-11T00:00:00Z'), now)
        ];
        """,
    )
    assert result == [
        "Pick when the range starts.",
        "The start cannot be in the future.",
        "The end has to be after the start.",
        "A range can be at most 366 days long.",
        None,
    ]


def test_quick_ranges_are_calendar_days_where_the_viewer_is(tmp_path: Path) -> None:
    """Thursday 12 November, 15:30 in India: "today" began at 18:30 UTC the day
    before, and the week on Monday the 9th."""
    result = run_js(
        tmp_path,
        """
        return W.quickRanges(Date.parse('2025-11-13T10:00:00Z')).map(function (entry) {
          return [entry.key, entry.from.toISOString(), entry.to ? entry.to.toISOString() : null];
        });
        """,
        tz="Asia/Kolkata",
    )
    assert result == [
        ["today", "2025-11-12T18:30:00.000Z", None],
        ["yesterday", "2025-11-11T18:30:00.000Z", "2025-11-12T18:30:00.000Z"],
        ["week", "2025-11-09T18:30:00.000Z", None],
        ["last-week", "2025-11-02T18:30:00.000Z", "2025-11-09T18:30:00.000Z"],
    ]


def test_a_datetime_input_means_local_time(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        """
        var instant = W.fromLocalInput('2025-11-10T08:00');
        return [instant.toISOString(), W.toLocalInput(instant)];
        """,
        tz="America/New_York",
    )
    assert result == ["2025-11-10T13:00:00.000Z", "2025-11-10T08:00"]


# ------------------------------------------------------------------ wording


def test_windows_are_said_in_words(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        """
        var six = W.parse('6h');
        var hour = W.parse('1h');
        // This year's dates, which are said without the year.
        var year = new Date().getFullYear();
        function local(m, d, h, min) { return new Date(year, m - 1, d, h || 0, min || 0); }
        var week = W.range(local(11, 2), local(11, 9));
        var day = W.range(local(11, 11), local(11, 12));
        var afternoon = W.range(local(11, 11, 8), local(11, 11, 18, 30));
        var lastYear = W.range(new Date(year - 1, 10, 2), new Date(year - 1, 10, 9));
        return {
          six: [W.title(six), W.during(six), W.briefly(six), W.across(six)],
          hour: [W.title(hour), W.during(hour), W.briefly(hour)],
          week: [W.label(week), W.during(week), W.briefly(week)],
          day: W.label(day),
          afternoon: W.label(afternoon),
          lastYear: [W.label(lastYear), String(year - 1)]
        };
        """,
    )
    assert result["six"] == [
        "Last 6 hours",
        "in the last 6 hours",
        "in 6 hours",
        "Across the last 6 hours",
    ]
    assert result["hour"] == ["Last hour", "in the last hour", "in 1 hour"]
    assert result["week"] == [f"Nov 2 {DASH} Nov 8", f"in Nov 2 {DASH} Nov 8", "in this range"]
    assert result["day"] == "Nov 11"
    assert plain(result["afternoon"]) == f"Nov 11, 8:00 AM {DASH} 6:30 PM"
    label, last_year = result["lastYear"]
    assert label == f"Nov 2, {last_year} {DASH} Nov 8, {last_year}", "another year says which"


def test_charts_say_their_bucket(tmp_path: Path) -> None:
    result = run_js(
        tmp_path,
        "return [86400, 3600, 900, 300, 172800].map(function (s) { return W.bucketWords(s); });",
    )
    assert [words["by"] for words in result] == [
        "by day",
        "by hour",
        "per 15 minutes",
        "per 5 minutes",
        "per 2 days",
    ]
    assert result[0]["gaps"] == "Days with no runs are gaps, not zeroes"
    assert result[2]["gaps"] == "15-minute slots with no runs are gaps, not zeroes"


def test_a_daily_bucket_is_a_utc_date_wherever_the_viewer_is(tmp_path: Path) -> None:
    """Regression: `new Date('2026-11-12')` is midnight UTC, which is the evening
    of the 11th in New York — every daily label there was a day early."""
    west = run_js(tmp_path, "return W.bucketLabel('2026-11-12', 86400);", tz="America/New_York")
    assert west == "Nov 12"
    # A clock bucket is an instant, shown in local time; at local midnight it
    # shows the date instead, so a day's boundary is visible on the axis.
    east = run_js(
        tmp_path,
        """
        return [W.bucketLabel('2026-11-12T18:30:00Z', 3600),
                W.bucketLabel('2026-11-12T19:30:00Z', 3600)];
        """,
        tz="Asia/Kolkata",
    )
    assert east[0] == "Nov 13"
    assert plain(east[1]) == "1:00 AM"


# ------------------------------------------- the browser buckets like the server


SPANS = [
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=6),
    timedelta(hours=24),
    timedelta(hours=48),
    timedelta(hours=49),
    timedelta(days=7),
    timedelta(days=30),
    timedelta(days=90),
    timedelta(days=365),
]


def test_the_trend_step_matches_the_server(tmp_path: Path) -> None:
    spans_ms = [int(span.total_seconds() * 1000) for span in SPANS]
    result = run_js(
        tmp_path,
        f"return {json.dumps(spans_ms)}.map(function (ms) {{ return W.trendStep(ms); }});",
    )
    assert result == [int(SqlRunRepository.trend_step(span).total_seconds()) for span in SPANS]


@pytest.mark.parametrize(
    "span",
    [
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
        timedelta(days=30),
        timedelta(days=90),
    ],
    ids=["1h", "6h", "24h", "30d", "90d"],
)
def test_a_filtered_trend_has_the_servers_buckets(tmp_path: Path, span: timedelta) -> None:
    """The same runs, bucketed by the browser and by the server, land in the
    same buckets under the same labels."""
    end = datetime(2025, 11, 12, 10, 37, 21, tzinfo=UTC)
    since = end - span
    step = SqlRunRepository.trend_step(span)
    moments = [since + span * fraction for fraction in (0.0, 0.03, 0.31, 0.32, 0.5, 0.77, 0.999)]
    rows = [
        SimpleNamespace(
            started_at=moment,
            created_at=moment,
            duration_ms=60_000 + index * 1000,
            counts={"total": 3 + index, "passed": 2 + index, "failed": 1},
        )
        for index, moment in enumerate(moments)
    ]
    expected = [
        [point.bucket, point.runs, point.total, point.failed]
        for point in SqlRunRepository._trend(rows, since, end, step)
    ]
    summaries = [
        {
            "started_at": row.started_at.isoformat(),
            "duration_ms": row.duration_ms,
            "counts": row.counts,
        }
        for row in rows
    ]
    result = run_js(
        tmp_path,
        f"""
        var summaries = {json.dumps(summaries)}.map(P.normalizeSummary);
        return P.buildTrend(summaries, {{
          start: Date.parse({json.dumps(since.isoformat())}),
          end: Date.parse({json.dumps(end.isoformat())}),
          step: {int(step.total_seconds())}
        }}).map(function (point) {{
          return [point.bucket, point.runs, point.total, point.failed];
        }});
        """,
    )
    assert result == expected
