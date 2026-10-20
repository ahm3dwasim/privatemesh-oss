"""The owner's held-out cases for `meeting_option_evaluator`.

THE POINT OF THIS FILE IS WHERE IT LIVES. It is not in the skill's directory, it was written
before any generation ran, it is never put in a prompt, and `skillrt.run_harness` refuses the
arrangement where the code being graded could rewrite it. A generator that writes its own tests has
proved that it can write tests; this is the other column.

The expectations are not hand-written. They were derived from `learn.reference_run` — which is
`fit.py`'s `check_slot`, plus the two rules `fit.py` does not model (`duration_minutes` and
`minimum_notice_minutes`), applied in the published order. `tests/test_learn_reference.py`
re-derives every one of them from that function, so if the trusted validator changes, this file
fails loudly instead of quietly grading the skill against a stale answer.

Each case is a top-level function taking no arguments, because that is what the isolated runner
calls: one process per case, so a skill that hangs or trips a violation cannot take the rest of the
suite down with it.

The module under test is imported by its manifest module name. It is not on `sys.path` from here —
the runtime puts the skill directory there for the duration of this import and removes it again
before the test function runs, which is what makes "the code cannot read the tests grading it" a
property of the rig rather than a promise.
"""
from __future__ import annotations

import json
import re

import meeting_option_evaluator

#: One HH:MM, so a rejection whose sentence names no clock time is caught. `fit.py`'s own rule is
#: that a refusal the owner cannot check is indistinguishable from a bug; a learned skill inherits
#: it. Only the presence of a time is asserted — the prose is the skill's own.
_TIME = re.compile(r"\d{2}:\d{2}")


#: The cases, as a JSON literal rather than as hand-indented Python: these values were DERIVED from
#: `learn.reference_run`, and a transcription step is a place for a wrong digit to hide. Keeping
#: them in the format they were produced in removes that step. `CASES` below is the parsed form.
_CASES_JSON = r"""
[
  {
    "name": "the_canonical_1600_is_refused_by_the_buffer",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T16:00:00+01:00",
          "end": "2026-09-19T16:45:00+01:00"
        },
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "overlaps_protected_buffer"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_slot_that_ends_exactly_at_the_buffer_start_still_fits",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_slot_that_starts_exactly_at_the_buffer_start_is_refused",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T15:45:00+01:00",
          "end": "2026-09-19T16:30:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "overlaps_protected_buffer"
      }
    ],
    "ranked": [],
    "venues": []
  },
  {
    "name": "a_slot_after_the_commitment_is_later_in_the_day_not_inside_the_buffer",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T17:30:00+01:00",
          "end": "2026-09-19T18:15:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T17:30:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_slot_inside_the_commitment_itself_is_refused",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T16:45:00+01:00",
          "end": "2026-09-19T17:30:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "overlaps_protected"
      }
    ],
    "ranked": [],
    "venues": []
  },
  {
    "name": "a_slot_that_has_already_ended_is_in_the_past",
    "inputs": {
      "now": "2026-09-19T11:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T09:00:00+01:00",
          "end": "2026-09-19T09:45:00+01:00"
        },
        {
          "start": "2026-09-19T14:00:00+01:00",
          "end": "2026-09-19T14:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "in_the_past"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T14:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_slot_before_the_day_starts_is_outside_the_day",
    "inputs": {
      "now": "2026-09-19T06:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T07:00:00+01:00",
          "end": "2026-09-19T07:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "outside_day"
      }
    ],
    "ranked": [],
    "venues": []
  },
  {
    "name": "a_slot_after_the_day_ends_is_outside_the_day",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T19:30:00+01:00",
          "end": "2026-09-19T20:15:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "outside_day"
      }
    ],
    "ranked": [],
    "venues": []
  },
  {
    "name": "a_slot_that_ends_before_it_starts_is_not_a_meeting",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:00:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "empty_slot"
      }
    ],
    "ranked": [],
    "venues": []
  },
  {
    "name": "a_slot_over_a_booked_interval_is_busy",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "busy": [
        {
          "start": "2026-09-19T12:00:00+01:00",
          "end": "2026-09-19T13:00:00+01:00"
        }
      ],
      "offered_slots": [
        {
          "start": "2026-09-19T12:30:00+01:00",
          "end": "2026-09-19T13:15:00+01:00"
        },
        {
          "start": "2026-09-19T13:00:00+01:00",
          "end": "2026-09-19T13:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "overlaps_busy"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T13:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_slot_of_the_wrong_length_is_refused",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "duration_minutes": 45,
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T16:00:00+01:00"
        },
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "wrong_duration"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_slot_sooner_than_the_notice_you_need_is_refused",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "minimum_notice_minutes": 120,
      "offered_slots": [
        {
          "start": "2026-09-19T10:00:00+01:00",
          "end": "2026-09-19T10:45:00+01:00"
        },
        {
          "start": "2026-09-19T11:30:00+01:00",
          "end": "2026-09-19T12:15:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "too_soon"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T11:30:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "a_zero_duration_and_zero_notice_state_no_constraint",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "duration_minutes": 0,
      "minimum_notice_minutes": 0,
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "the_rules_the_validator_owns_are_decided_before_the_ones_it_does_not",
    "inputs": {
      "now": "2026-09-19T11:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "duration_minutes": 30,
      "offered_slots": [
        {
          "start": "2026-09-19T09:00:00+01:00",
          "end": "2026-09-19T09:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "in_the_past"
      }
    ],
    "ranked": [],
    "venues": []
  },
  {
    "name": "fitting_slots_are_ranked_earliest_first_whatever_order_they_were_offered",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T17:00:00+01:00",
          "end": "2026-09-19T17:45:00+01:00"
        },
        {
          "start": "2026-09-19T14:00:00+01:00",
          "end": "2026-09-19T14:45:00+01:00"
        },
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      },
      {
        "fits": true,
        "code": "fits"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T14:00:00+01:00",
      "2026-09-19T15:00:00+01:00",
      "2026-09-19T17:00:00+01:00"
    ],
    "venues": [
      null,
      null,
      null
    ]
  },
  {
    "name": "only_the_slots_that_fit_are_ranked",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T16:00:00+01:00",
          "end": "2026-09-19T16:45:00+01:00"
        },
        {
          "start": "2026-09-19T17:30:00+01:00",
          "end": "2026-09-19T18:15:00+01:00"
        },
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "overlaps_protected_buffer"
      },
      {
        "fits": true,
        "code": "fits"
      },
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00",
      "2026-09-19T17:30:00+01:00"
    ],
    "venues": [
      null,
      null
    ]
  },
  {
    "name": "the_first_official_source_is_the_venue",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ],
      "venues": [
        {
          "name": "A Blog Roundup",
          "title": "10 cafes",
          "url": "https://blog.example/",
          "excerpt": "a list",
          "official": false
        },
        {
          "name": "The Standard",
          "title": "The Standard, King's Cross",
          "url": "https://thestandard.co.uk/",
          "excerpt": "Open daily 12:00-23:00",
          "official": true
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      "The Standard"
    ]
  },
  {
    "name": "no_official_source_means_no_venue_rather_than_a_guess",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ],
      "venues": [
        {
          "name": "A Blog Roundup",
          "title": "10 cafes",
          "url": "https://blog.example/",
          "excerpt": "a list",
          "official": false
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "no_venue_evidence_at_all_is_not_an_error",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "offered_slots": [
        {
          "start": "2026-09-19T15:00:00+01:00",
          "end": "2026-09-19T15:45:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": true,
        "code": "fits"
      }
    ],
    "ranked": [
      "2026-09-19T15:00:00+01:00"
    ],
    "venues": [
      null
    ]
  },
  {
    "name": "nothing_fits_at_all",
    "inputs": {
      "now": "2026-09-19T09:00:00+01:00",
      "day_start": "2026-09-19T08:00:00+01:00",
      "day_end": "2026-09-19T20:00:00+01:00",
      "protected": {
        "start": "2026-09-19T16:30:00+01:00",
        "end": "2026-09-19T17:30:00+01:00",
        "buffer_minutes": 45
      },
      "offered_slots": [
        {
          "start": "2026-09-19T16:00:00+01:00",
          "end": "2026-09-19T16:45:00+01:00"
        },
        {
          "start": "2026-09-19T16:45:00+01:00",
          "end": "2026-09-19T17:30:00+01:00"
        }
      ]
    },
    "options": [
      {
        "fits": false,
        "code": "overlaps_protected_buffer"
      },
      {
        "fits": false,
        "code": "overlaps_protected"
      }
    ],
    "ranked": [],
    "venues": []
  }
]
"""

CASES: dict[str, dict] = {case["name"]: case for case in json.loads(_CASES_JSON)}

def _check(case: str) -> None:
    """Run one case and hold the skill to the trusted answer, field by field."""
    spec = CASES[case]
    result = meeting_option_evaluator.run(spec["inputs"])

    options = result["options"]
    assert len(options) == len(spec["options"]), (
        f"{case}: {len(options)} options for {len(spec['options'])} offered slots")
    for i, (got, want) in enumerate(zip(options, spec["options"])):
        assert got["fits"] == want["fits"], (
            f"{case}: option {i} fits={got['fits']}, the trusted validator says {want['fits']}")
        assert got["code"] == want["code"], (
            f"{case}: option {i} was decided by {got['code']!r}, not {want['code']!r}")
        assert isinstance(got["reason"], str) and got["reason"].strip(), (
            f"{case}: option {i} has no reason")
        assert _TIME.search(got["reason"]), (
            f"{case}: option {i}'s reason names no clock time: {got['reason']!r}")

    assert [r["start"] for r in result["ranked"]] == spec["ranked"], (
        f"{case}: ranked {[r['start'] for r in result['ranked']]}, expected {spec['ranked']}")
    assert [r["venue"]["name"] if r["venue"] else None for r in result["ranked"]] == spec["venues"], (
        f"{case}: venue attachments differ from the expected {spec['venues']}")


def test_the_canonical_1600_is_refused_by_the_buffer():
    _check("the_canonical_1600_is_refused_by_the_buffer")


def test_a_slot_that_ends_exactly_at_the_buffer_start_still_fits():
    _check("a_slot_that_ends_exactly_at_the_buffer_start_still_fits")


def test_a_slot_that_starts_exactly_at_the_buffer_start_is_refused():
    _check("a_slot_that_starts_exactly_at_the_buffer_start_is_refused")


def test_a_slot_after_the_commitment_is_later_in_the_day_not_inside_the_buffer():
    _check("a_slot_after_the_commitment_is_later_in_the_day_not_inside_the_buffer")


def test_a_slot_inside_the_commitment_itself_is_refused():
    _check("a_slot_inside_the_commitment_itself_is_refused")


def test_a_slot_that_has_already_ended_is_in_the_past():
    _check("a_slot_that_has_already_ended_is_in_the_past")


def test_a_slot_before_the_day_starts_is_outside_the_day():
    _check("a_slot_before_the_day_starts_is_outside_the_day")


def test_a_slot_after_the_day_ends_is_outside_the_day():
    _check("a_slot_after_the_day_ends_is_outside_the_day")


def test_a_slot_that_ends_before_it_starts_is_not_a_meeting():
    _check("a_slot_that_ends_before_it_starts_is_not_a_meeting")


def test_a_slot_over_a_booked_interval_is_busy():
    _check("a_slot_over_a_booked_interval_is_busy")


def test_a_slot_of_the_wrong_length_is_refused():
    _check("a_slot_of_the_wrong_length_is_refused")


def test_a_slot_sooner_than_the_notice_you_need_is_refused():
    _check("a_slot_sooner_than_the_notice_you_need_is_refused")


def test_a_zero_duration_and_zero_notice_state_no_constraint():
    _check("a_zero_duration_and_zero_notice_state_no_constraint")


def test_the_rules_the_validator_owns_are_decided_before_the_ones_it_does_not():
    _check("the_rules_the_validator_owns_are_decided_before_the_ones_it_does_not")


def test_fitting_slots_are_ranked_earliest_first_whatever_order_they_were_offered():
    _check("fitting_slots_are_ranked_earliest_first_whatever_order_they_were_offered")


def test_only_the_slots_that_fit_are_ranked():
    _check("only_the_slots_that_fit_are_ranked")


def test_the_first_official_source_is_the_venue():
    _check("the_first_official_source_is_the_venue")


def test_no_official_source_means_no_venue_rather_than_a_guess():
    _check("no_official_source_means_no_venue_rather_than_a_guess")


def test_no_venue_evidence_at_all_is_not_an_error():
    _check("no_venue_evidence_at_all_is_not_an_error")


def test_nothing_fits_at_all():
    _check("nothing_fits_at_all")
