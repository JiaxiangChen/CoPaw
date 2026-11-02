export type HtmlPreviewEventType =
  | "button_click"
  | "preview_view"
  | "module_exposure";
export type HtmlPreviewTemplateType = "main" | "sub";

export interface HtmlTrackerPayloadType {
  source_id?: string | null;
  user_id?: string | null;
  user_name?: string | null;
  bbk_id?: string | null;
  cron_task_id?: string | null;
  cron_task_name?: string | null;
  file_url?: string;
  file_name?: string | null;
  list_key?: string | null;
  list_name?: string | null;
  button_id?: string | null;
  button_name?: string | null;
  button_text?: string | null;
  button_type?: "insight" | "phone" | "plan" | "other" | string | null;
  customer_id?: string | null;
  customer_name?: string | null;
  customer_info?: Record<string, string> | null;
  clicked_at?: string | null;
  trace_id?: string | null;
  event_type?: HtmlPreviewEventType;
  template_type?: HtmlPreviewTemplateType | null;
  template_id?: number | null;
  result_id?: string | null;
  event_target_id?: string | null;
  event_target_name?: string | null;
  page_source?: string | null;
  platform_source?: string | null;
}

export interface HtmlPreviewClickSubmitResponse {
  success: boolean;
}

export interface HtmlPreviewClickSummaryItem {
  button_label: string;
  button_id?: string | null;
  button_name?: string | null;
  button_text?: string | null;
  bbk_id?: string | null;
  cron_task_id?: string | null;
  cron_task_name?: string | null;
  list_key?: string | null;
  list_name?: string | null;
  file_url?: string | null;
  file_name?: string | null;
  click_count: number;
  last_clicked_at?: string | null;
}

export interface HtmlPreviewClickSummaryResponse {
  success: boolean;
  items: HtmlPreviewClickSummaryItem[];
}

export interface HtmlPreviewClickEventItem {
  id: number;
  source_id?: string | null;
  user_id?: string | null;
  user_name?: string | null;
  bbk_id?: string | null;
  cron_task_id?: string | null;
  cron_task_name?: string | null;
  file_url: string;
  file_name?: string | null;
  list_key?: string | null;
  list_name?: string | null;
  button_id?: string | null;
  button_name?: string | null;
  button_text?: string | null;
  button_type?: string | null;
  customer_id?: string | null;
  customer_name?: string | null;
  customer_info?: Record<string, string> | null;
  clicked_at?: string | null;
  event_type?: HtmlPreviewEventType;
  template_type?: HtmlPreviewTemplateType | null;
  template_id?: number | null;
  result_id?: string | null;
  event_target_id?: string | null;
  event_target_name?: string | null;
  trace_id?: string | null;
  page_source?: string | null;
  platform_source?: string | null;
}

export interface HtmlPreviewClickEventListResponse {
  success: boolean;
  items: HtmlPreviewClickEventItem[];
}

export interface HtmlPreviewCustomerClickSummaryItem {
  customer_id?: string | null;
  customer_name: string;
  insight_count: number;
  phone_count: number;
  plan_count: number;
  total_click_count: number;
  last_clicked_user_id?: string | null;
  last_clicked_user_name?: string | null;
  last_clicked_at?: string | null;
}

export interface HtmlPreviewCustomerClickSummaryResponse {
  success: boolean;
  items: HtmlPreviewCustomerClickSummaryItem[];
}

export interface HtmlPreviewListSnapshotCustomer {
  customer_id?: string | null;
  customer_name: string;
  extra_info?: Record<string, string> | null;
}

export interface HtmlPreviewListSnapshotPayload {
  source_id?: string | null;
  bbk_id?: string | null;
  cron_task_id?: string | null;
  cron_task_name?: string | null;
  list_key?: string | null;
  list_name?: string | null;
  file_url: string;
  file_name?: string | null;
  customers: HtmlPreviewListSnapshotCustomer[];
  snapshot_at?: string | null;
}

export interface HtmlPreviewListSnapshotResponse {
  success: boolean;
  customer_count: number;
}

export interface HtmlPreviewListSummaryItem {
  list_key: string;
  list_name: string;
  file_url?: string | null;
  file_name?: string | null;
  cron_task_id?: string | null;
  cron_task_name?: string | null;
  customer_count: number;
  clicked_customer_count: number;
  insight_count: number;
  phone_count: number;
  plan_count: number;
  total_click_count: number;
  last_clicked_at?: string | null;
}

export interface HtmlPreviewListSummaryResponse {
  success: boolean;
  total?: number;
  clicked_list_count?: number;
  page?: number;
  page_size?: number;
  summary?: HtmlPreviewListSummaryItem;
  items: HtmlPreviewListSummaryItem[];
}

export interface HtmlPreviewCustomerClickItem {
  customer_id?: string | null;
  customer_name: string;
  list_key?: string | null;
  list_name?: string | null;
  insight_count: number;
  phone_count: number;
  plan_count: number;
  total_click_count: number;
  last_clicked_user_id?: string | null;
  last_clicked_user_name?: string | null;
  manager_clicks?: HtmlPreviewCustomerManagerClickItem[];
  last_clicked_at?: string | null;
}

export interface HtmlPreviewCustomerManagerClickItem {
  user_id: string;
  user_name?: string | null;
  insight_count: number;
  phone_count: number;
  plan_count: number;
  total_click_count: number;
  last_clicked_at?: string | null;
}

export interface HtmlPreviewCustomerClickResponse {
  success: boolean;
  items: HtmlPreviewCustomerClickItem[];
}
