import { useState, useCallback, useEffect, useRef } from "react";
import {
  marketApi,
  Category,
  MarketSkill,
  MarketSkillDetail,
  MarketBrowseResponse,
  ORPHANED_CATEGORY_ID,
  UNCATEGORIZED_CATEGORY_ID,
} from "../../api/modules/market";

export function useMarket(sourceId: string) {
  const [categories, setCategories] = useState<Category[]>([]);
  const [skills, setSkills] = useState<MarketSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const [statsStatus, setStatsStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const skillsRequestId = useRef(0);
  const [selectedCategory, setSelectedCategory] = useState<number | null>(null);
  const [selectedBbkId, setSelectedBbkId] = useState<string | null>(null);
  const [selectedSkill, setSelectedSkill] = useState<MarketSkillDetail | null>(
    null,
  );
  const [detailDrawerOpen, setDetailDrawerOpen] = useState(false);
  const [publishModalOpen, setPublishModalOpen] = useState(false);
  const [browse, setBrowse] = useState<MarketBrowseResponse | null>(null);

  useEffect(
    () => () => {
      skillsRequestId.current += 1;
    },
    [sourceId, selectedCategory, selectedBbkId],
  );

  const refreshCategories = useCallback(async () => {
    try {
      const data = await marketApi.listCategories(sourceId);
      setCategories(data);
    } catch (err) {
      console.error("Failed to load categories:", err);
    }
  }, [sourceId]);

  const refreshSkills = useCallback(async () => {
    const requestId = ++skillsRequestId.current;
    setLoading(true);
    setStatsStatus("loading");
    const options = {
      categoryId:
        selectedCategory === UNCATEGORIZED_CATEGORY_ID
          ? null
          : selectedCategory,
      bbkId: selectedBbkId,
      uncategorized: selectedCategory === UNCATEGORIZED_CATEGORY_ID,
      orphaned: selectedCategory === ORPHANED_CATEGORY_ID,
    };
    try {
      const data = await marketApi.browseMarket(sourceId, "skill", {
        ...options,
        includeStats: false,
      });
      if (requestId !== skillsRequestId.current) return;
      setBrowse(data);
      setSkills(data.items as MarketSkill[]);
      if (!data.items.length) {
        setStatsStatus("ready");
        return;
      }
      void marketApi
        .browseMarket(sourceId, "skill", {
          ...options,
          includeStats: true,
        })
        .then((statistics) => {
          if (requestId !== skillsRequestId.current) return;
          if (statistics.stats_available === false) {
            setStatsStatus("error");
            return;
          }
          const byId = new Map(
            statistics.items.map((item) => [item.item_id, item]),
          );
          setSkills((current) =>
            current.map((skill) => {
              const stats = byId.get(skill.item_id);
              return stats
                ? {
                    ...skill,
                    call_count: stats.call_count,
                    user_count: stats.user_count,
                  }
                : skill;
            }),
          );
          setStatsStatus("ready");
        })
        .catch((err) => {
          if (requestId !== skillsRequestId.current) return;
          console.error("Failed to load skill statistics:", err);
          setStatsStatus("error");
        });
    } catch (err) {
      if (requestId !== skillsRequestId.current) return;
      console.error("Failed to load skills:", err);
      setStatsStatus("error");
    } finally {
      if (requestId === skillsRequestId.current) setLoading(false);
    }
  }, [sourceId, selectedCategory, selectedBbkId]);

  // 刷新当前选中技能的详情
  const refreshSelectedSkill = useCallback(async () => {
    if (!selectedSkill) return;
    try {
      const detail = await marketApi.getSkillDetail(
        sourceId,
        selectedSkill.item_id,
      );
      if (detail) {
        setSelectedSkill(detail);
      }
    } catch (err) {
      console.error("Failed to refresh skill detail:", err);
    }
  }, [sourceId, selectedSkill]);

  // 刷新技能列表和详情
  const refreshSkillsAndDetail = useCallback(async () => {
    await refreshSkills();
    await refreshSelectedSkill();
  }, [refreshSkills, refreshSelectedSkill]);

  const openSkillDetail = useCallback(
    async (itemId: string) => {
      try {
        const detail = await marketApi.getSkillDetail(sourceId, itemId);
        if (detail) {
          setSelectedSkill(detail);
          setDetailDrawerOpen(true);
        }
      } catch (err) {
        console.error("Failed to load skill detail:", err);
      }
    },
    [sourceId],
  );

  return {
    categories,
    skills,
    loading,
    statsStatus,
    browse,
    selectedCategory,
    setSelectedCategory,
    selectedBbkId,
    setSelectedBbkId,
    selectedSkill,
    detailDrawerOpen,
    setDetailDrawerOpen,
    publishModalOpen,
    setPublishModalOpen,
    refreshCategories,
    refreshSkills,
    refreshSelectedSkill,
    refreshSkillsAndDetail,
    openSkillDetail,
  };
}
