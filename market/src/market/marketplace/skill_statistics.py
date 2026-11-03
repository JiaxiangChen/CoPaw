# -*- coding: utf-8 -*-
"""Shared calendar-month filters for marketplace skill statistics."""

from datetime import datetime

EXCLUDED_SKILL_NAMES = (
    "cron",
    "search_customs_by_labels",
    "cust_insight_url_generator",
    "immortal-skill",
    "batch_task_executor",
    "skill_creator",
    "docx",
    "himalaya",
)

SKILL_STATS_WHERE = """
    source_id = %s
      AND start_time >= %s AND start_time < %s
      {bbk_filter_sql}
      AND user_id != 'default'
      AND skill_name NOT IN ({excluded_skill_placeholders})
      AND (
          NOT EXISTS (
              SELECT 1 FROM swe_marketplace_skills
              WHERE source_id = %s AND include_in_statistics = 1
          )
          OR EXISTS (
              SELECT 1
              FROM swe_marketplace_skills market_skill
              WHERE market_skill.source_id = swe_tracing_spans.source_id
                AND market_skill.skill_name = swe_tracing_spans.skill_name
                AND market_skill.include_in_statistics = 1
          )
      )
"""


def build_skill_stats_filter(
    source_id: str,
    user_bbk_id: str,
) -> tuple[str, tuple]:
    """Use the request branch, with unrestricted scope for HQ and V00."""
    month_start = datetime.now().replace(
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    month_end = month_start.replace(
        year=month_start.year + (month_start.month == 12),
        month=month_start.month % 12 + 1,
    )
    branch_params = () if user_bbk_id in {"100", "V00"} else (user_bbk_id,)
    where = SKILL_STATS_WHERE.format(
        bbk_filter_sql="AND bbk_id = %s" if branch_params else "",
        excluded_skill_placeholders=", ".join(
            ["%s"] * len(EXCLUDED_SKILL_NAMES),
        ),
    )
    return where, (
        source_id,
        month_start,
        month_end,
        *branch_params,
        *EXCLUDED_SKILL_NAMES,
        source_id,
    )
