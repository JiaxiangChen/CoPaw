import { Routes, Route } from "react-router-dom";
import CronOverviewPage from "./CronOverview";
import CronBatchDispatchPage from "./CronBatchDispatch";
import ManagedBatchDispatchPage from "./ManagedBatchDispatch";
import TaskCenterPage from "./TaskCenter";
import CronTaskTracking from "./CronTaskTracking";

export default function MonitorPage() {
  return (
    <Routes>
      <Route path="/" element={<CronOverviewPage />} />
      <Route path="/cron-overview" element={<CronOverviewPage />} />
      <Route path="/cron-batch-dispatch" element={<CronBatchDispatchPage />} />
      <Route
        path="/cron-batch-dispatch-new"
        element={<ManagedBatchDispatchPage />}
      />
      <Route path="/tasks" element={<TaskCenterPage />} />
      <Route path="/cron-task-tracking" element={<CronTaskTracking />} />
    </Routes>
  );
}
