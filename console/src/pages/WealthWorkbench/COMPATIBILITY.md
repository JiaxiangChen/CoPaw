# 财富工作台浏览器兼容性

本记录范围仅限 `/wealth/*`。项目统一要求兼容 **360 浏览器极速模式，Chromium 内核 108 及以上**，详见 [Console 浏览器兼容规范](../../../DESIGN.md#browser-compatibility)。Chromium 80–107 按实际问题尽力兼容，不作为必须通过的验收基线；IE 内核兼容模式不在支持范围内。检查以 Chromium 功能版本为准，不以 360 品牌或 User-Agent 字符串代替能力验证。

## 已检查的样式和原生能力

| 能力                                           | Chromium 起始支持版本 | 工作台处理                                                                                                                        |
| ---------------------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `subgrid`                                      | 117                   | 使用普通 Grid；场景和方向合并，执行周期与督办策略各自分组。配置行内控件横向排列，超宽时局部滚动，整表不横向溢出。                           |
| `inset`                                        | 87                    | 顶栏和 Toast 改用 `top` / `right` / `bottom` / `left`。                                                                           |
| `:focus-visible`                               | 86                    | 使用通用的 `:focus` 保留按钮、输入和可聚焦内容的轮廓；客户数输入通过父控件 `:focus-within` 显示状态。鼠标点击也可能显示焦点轮廓。 |
| 普通 Grid、`minmax()`、`max-content`、CSS 变量 | 80 之前               | 保留；普通 Grid 不等于 `subgrid`。                                                                                                |
| `min()` / `max()` / `clamp()`                  | 79                    | 保留，不需要仅因写法较新而替换。                                                                                                  |
| Flex `gap`                                     | 84                    | 按用户确认暂不整改。80–83 会丢失 Flex 项之间的间距；Grid 的 gap 已支持。不能据此宣称 80–83 的视觉效果完全一致。                   |
| Popover API                                    | 114                   | 现有 Toast 同时检测 `showPopover`、`hidePopover`；缺失时不设置 `popover` 属性，使用原有显隐和弹窗内 Portal。                      |
| `AbortSignal.throwIfAborted()`                 | 100                   | 现有 `abortSignal.ts` 已使用 `aborted` 和兼容的取消异常处理。                                                                     |

工作台内未发现 `:has()`、容器查询、`color-mix()`、动态视口单位等依赖；其他 Console 页面的扫描结果不属于本次整改范围。

## 维护约定

- 关键表格、顶栏和表单布局优先使用普通 Grid、Flex、明确的列宽与常规定位属性。
- 日期框等第三方控件的尺寸优先通过自有类名控制，不仅匹配 `.ant-*`；正式 App 使用 `swe` 前缀。浏览器验证必须带上正式 ConfigProvider、主题和全局样式，并检查 `swe` / `ant` 前缀下的实际控件边界。
- 不新增仅依赖超出 Chromium 108 基线的特性才能工作的关键布局；使用较新能力前检查实际支持版本，必要时提供兼容实现。原生 API 需确认目标内核支持，必要时显式检查方法是否存在。
- Vite 的 `target` / `cssTarget` 不能为所有 CSS 特性或原生 API 自动补齐兼容实现。
- 排查 360 问题时记录实际 Chromium 版本和极速/兼容模式；本项目的 React、Ant Design 和 CSS 变量等依赖不构成 IE 内核兼容方案。

## 验证入口

```bash
cd console
npm run test:run -- src/pages/WealthWorkbench src/App.wealth.test.ts --exclude '**/api.test.ts'
npm run typecheck
NODE_OPTIONS=--max-old-space-size=8192 npm run build
```

浏览器检查覆盖创建页普通/自定义周期、多种频率、长场景名、督办策略接口回显、客户数、删除和空状态，以及看板和任务页的 1280、1440、1920 宽度、隐藏头部、键盘焦点、弹窗及缺少 Popover API 的 Toast。模拟禁用新能力只能验证对应分支，不能替代原生 Windows 360 Chromium 108 的基线验证；本次整改尚未执行原生 360 复测。

兼容数据参考：[MDN 浏览器兼容数据](https://github.com/mdn/browser-compat-data)、[Chrome 117 的 subgrid](https://developer.chrome.com/blog/new-in-chrome-117/)。

督办策略的接口字段、角色限制及规划回显见 [监控配置维护记录](../../../../analysis/playbook/wealth-scene-monitoring.md)。
