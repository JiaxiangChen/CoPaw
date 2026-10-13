import { useEffect } from "react";
import { useWealthStore } from "./store";

/** iframe 文档随浏览器标签页切换可见性；宿主内部页签需另行通知。 */
export function useVisibilityRefresh() {
  useEffect(() => {
    let wasHidden = document.visibilityState === "hidden";
    const handleVisibilityChange = () => {
      const hidden = document.visibilityState === "hidden";
      if (wasHidden && !hidden) {
        void useWealthStore.getState().refreshTaskLists();
      }
      wasHidden = hidden;
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
    };
  }, []);
}
