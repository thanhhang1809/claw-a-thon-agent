"""
Shared data models. CHỐT SCHEMA NÀY VỚI PERSON 2 TRƯỚC KHI CODE.
Person 1 (I/O) produces Ticket. Person 2 (Rules) consumes Ticket, produces DailyReport.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from enum import IntEnum
from typing import Optional


# ---------------------------------------------------------------------------
# STATUS GROUPS  (workflow ZaloPay — dùng chung Person 1 & 2, so lower-case)
# Full flow: NEW > REVIEWED > IN ANALYSIS > READY FOR DEV > IN DEV >
#            WALKTHROUGH > READY FOR TESTING > IN TEST > IN REVIEW > DONE > LIVE
#            + BLOCKED / ON HOLD, CANCELLED (nhánh riêng)
# ---------------------------------------------------------------------------
STATUS_BLOCKED   = {"blocked / on hold", "blocked", "on hold"}
STATUS_TESTING   = {"in test"}                       # đang test
STATUS_PRE_TEST  = {"ready for testing"}             # chờ test, chưa bắt đầu
STATUS_POST_TEST = {"in review", "done", "live"}     # đã qua test
STATUS_CLOSED    = {"done", "live", "cancelled"}     # coi như xong/đóng

# ---------------------------------------------------------------------------
# COMPONENT -> TEAM ROUTING
# ---------------------------------------------------------------------------
COMPONENT_MS  = "Marketing Solutions"
COMPONENT_CRM = "CRM"

# Tiền tố tên sprint để lọc team. Sprint đặt tên: "MS - Sprint 26.04.A", "CRM - Sprint 26.05.B"
SPRINT_PREFIX_MS  = "MS -"
SPRINT_PREFIX_CRM = "CRM -"

# Channel keys (mỗi key = 1 webhook URL riêng)
CH_QE      = "qe"
CH_DEV_MS  = "dev_ms"
CH_DEV_CRM = "dev_crm"


def dev_channels_for(component: Optional[str]) -> list[str]:
    """Ticket -> (các) dev channel. Không thuộc MS/CRM -> gửi cả hai."""
    if component == COMPONENT_MS:
        return [CH_DEV_MS]
    if component == COMPONENT_CRM:
        return [CH_DEV_CRM]
    return [CH_DEV_MS, CH_DEV_CRM]


def ticket_in_team_sprint(sprints: list[str], team: str) -> bool:
    """team = 'MS' | 'CRM'. True nếu ticket thuộc ít nhất 1 sprint của team đó."""
    prefix = SPRINT_PREFIX_MS if team == "MS" else SPRINT_PREFIX_CRM
    return any(s.strip().upper().startswith(prefix.upper()) for s in sprints)


# ---------------------------------------------------------------------------
# TICKET DTO  (Person 1 output -> Person 2 input)
# ---------------------------------------------------------------------------
@dataclass
class Ticket:
    id: str
    title: str
    status: str                              # e.g. "In Progress", "Testing", "Done"
    story_point: Optional[float]
    test_start_date: Optional[date]
    test_complete_date: Optional[date]
    sandbox_date: Optional[date]
    assignee: str                            # displayName (hiển thị)
    assignee_username: Optional[str] = None  # username/email — để @mention qua Graph
    qe_pic: Optional[str] = None             # QC PIC displayName
    qe_pic_username: Optional[str] = None    # QC PIC username/email — để @mention
    no_qe: bool = False                      # NoQE flag (từ label)
    blocked: bool = False
    is_bug: bool = False                     # roadmap filter includes bugs
    component: Optional[str] = None          # component name (routing MS/CRM)
    sprints: list[str] = field(default_factory=list)  # tên các sprint ticket thuộc

    def __repr__(self) -> str:
        return f"<Ticket {self.id} {self.status!r} sp={self.story_point}>"


# ---------------------------------------------------------------------------
# RULE RESULT  (Person 2 internal -> DailyReport)
# ---------------------------------------------------------------------------
class Level(IntEnum):
    DATA = 0      # Level 0 — thiếu ngày (sandbox/test start/complete)
    VIOLENT = 1   # Level 1
    RISK = 2      # Level 2
    COMMIT = 3    # Level 3


@dataclass
class RuleResult:
    rule_id: str
    level: Level
    ticket: Ticket
    reason: str                              # short human text shown in card
    # "gửi đâu": Person 2 set theo mindmap. CH_QE và/hoặc dev (dùng "dev"
    # nghĩa là theo component MS/CRM). Person 1 expand "dev" -> dev_ms/dev_crm.
    send_qe: bool = True                     # gửi QE daily channel?
    send_dev: bool = False                   # gửi Dev channel (theo component)?
    mention_assignee: bool = False           # tag thêm assignee (vd sandbox, blocked)


# ---------------------------------------------------------------------------
# DAILY REPORT  (Person 2 output -> Person 1 renders to Teams)
# ---------------------------------------------------------------------------
@dataclass
class DailyReport:
    report_date: date
    # simple filter lists (Module 2C) -------------------------------------
    need_start_today: list[Ticket] = field(default_factory=list)
    need_complete_today: list[Ticket] = field(default_factory=list)
    sandbox_tomorrow: list[Ticket] = field(default_factory=list)
    blocked: list[Ticket] = field(default_factory=list)
    # rule matches grouped by level (Module 2B) ---------------------------
    level0: list[RuleResult] = field(default_factory=list)  # DATA — thiếu ngày
    level1: list[RuleResult] = field(default_factory=list)
    level2: list[RuleResult] = field(default_factory=list)
    level3: list[RuleResult] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.need_start_today, self.need_complete_today,
            self.sandbox_tomorrow, self.blocked,
            self.level0, self.level1, self.level2, self.level3,
        ])
