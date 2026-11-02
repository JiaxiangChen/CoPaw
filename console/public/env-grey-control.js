/*
 * ============================================================
 * Author: Kun He
 * Description: 运行时配置
 * Date: 2026-04-07
 * ============================================================
 */
window.__env__ = {
  baseUrl: "", // nginx将动态替换这里的内容
  serviceUnitId: '',
  env: '',
  systemCode: '',
  systemSect: '',
  responseFeedbackUserWhitelist: ["*"], // 回答反馈卡片白名单，"*"表示全员开放
  voiceRecorderUserWhitelist: ["*"], // 语音录制按钮白名单，"*"表示全员开放
  directAccessUserWhitelist: [], // 顶层窗口直接访问白名单；空数组表示全部拒绝，不支持"*"
  operationsUsersByBranch: { V00001: ["80008888"] }, // 财富工作台运维入口白名单，按真实分行匹配用户编号；空对象禁用，不支持"*"
  chatSessionPageSize: 100, // 聊天历史分页大小，未配置或非法时默认100
};
