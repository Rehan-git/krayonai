"""Which board contest feeds each race_id (see docs/phase1_report.md).

Contest values are per election page and get reused across elections, so
every entry also carries the exact label expected in the page's contest
dropdown. The fetcher refuses a contest whose label doesn't match, rather
than saving the wrong race under a race_id.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TURNOUT = "turnout"   # pseudo race_id for the Registered Voters/Ballots/Turnout "contest"


@dataclass(frozen=True)
class Contest:
    race_id: str          # a race_id from the Sheet's Races tab, or TURNOUT
    value: str            # the contest <option value> on the election page
    label: str            # the exact <option> text expected

    @property
    def label_re(self) -> re.Pattern:
        return re.compile(r"^\s*" + re.escape(self.label) + r"\s*$")


@dataclass(frozen=True)
class Election:
    election_id: str
    page_id: str
    election_date: str
    contests: tuple[Contest, ...]


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


_SUBDISTRICTS = [f"{d}{s}" for d in range(1, 11) for s in "AB"]   # 1A, 1B, ... 10B

ELECTIONS: dict[str, Election] = {
    "2026g": Election("2026g", "42", "2026-11-03", (
        Contest(TURNOUT, "", "Registered Voters/Ballots/Turnout"),
        Contest("2026g-GOV", "12", "Governor & Lieutenant Governor, Illinois"),
        Contest("2026g-SOS", "14", "Secretary of State, State of Illinois"),
        Contest("2026g-SBPRES", "121", "President, Board of Education"),
        *(Contest(f"2026g-SB{sd[:-1].zfill(2)}{sd[-1]}", str(122 + i),
                  f"Member, Board of Education, Subdistrict {sd}")
          for i, sd in enumerate(_SUBDISTRICTS)),
    )),
    "2024g": Election("2024g", "41", "2024-11-05", (
        Contest(TURNOUT, "", "Registered Voters/Ballots/Turnout"),
        *(Contest(f"2024g-SB{d:02d}", str(109 + d),
                  f"Member of the Chicago Board of Education, {_ordinal(d)} District")
          for d in range(1, 11)),
    )),
}

# Source documents fetched alongside the results (QA references).
DOCUMENTS: dict[str, list[tuple[str, str]]] = {
    "2024g": [("https://www.chicagoelections.gov/media/1337", "proclamation_2024-11-05.pdf")],
    "2026g": [("https://cboeprod.blob.core.usgovcloudapi.net/prod/2026-02/"
               "District%20Schedule%20Chicago%20Elected%20School%20Board%202026.pdf",
               "school_board_district_schedule_2026-02-09.pdf")],
}
