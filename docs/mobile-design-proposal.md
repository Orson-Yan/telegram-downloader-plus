# Telegram Downloader WebUI 移动端设计方案

## 现状分析

### 当前技术栈
- TailwindCSS (CDN) + Alpine.js (CDN) + 单文件 index.html (1381 行)
- 图标全部 inline SVG，无图标库
- Favicon: 内联 SVG 数据 URI（蓝底白箭头）

### 移动端现有问题

| 问题 | 现状 | 影响 |
|------|------|------|
| Favicon/图标 | 内联 SVG，无多尺寸图标 | 添加到主屏幕后图标模糊或缺失 |
| PWA 支持 | 无 manifest.json | 无法作为 Web App 安装到主屏幕 |
| 浏览器主题色 | 未设置 | 状态栏颜色不匹配 |
| 4 个 Tab 导航 | 水平文字 + badge，无溢出处理 | 窄屏下可能挤压/溢出 |
| 卡片信息密度 | 一行塞 chat、task_id、时间、速度 | 手机上字号小，不易阅读 |
| 操作按钮 | 右对齐小图标按钮 | 触摸目标偏小 |
| 批量操作栏 | 桌面级内联布局 | 手机上按钮过多会换行 |
| 删除确认弹窗 | max-w-md mx-4 | 基本可用但缺少 safe area 适配 |
| Toast 位置 | bottom-4 right-4 | 可能遮挡 iOS 底部安全区 |
| 搜索栏 | max-w-md | 手机上太窄，浪费横向空间 |
| 无下拉刷新 | 依赖 setInterval 轮询 | 用户无法手动触发刷新 |

---

## 设计方案

### 1. PWA 与图标

#### 1.1 添加 Web App Manifest (`manifest.json`)

```json
{
  "name": "Telegram Downloader",
  "short_name": "TG Downloader",
  "description": "Telegram Media Downloader",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#f9fafb",
  "theme_color": "#1e40af",
  "orientation": "portrait",
  "icons": [
    { "src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png" },
    { "src": "/static/icons/icon-maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable" },
    { "src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable" }
  ]
}
```

#### 1.2 HTML head 增加 meta 标签

```html
<meta name="theme-color" content="#1e40af">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="TG下载">
<link rel="apple-touch-icon" href="/static/icons/icon-180.png">
<link rel="manifest" href="/manifest.json">
```

#### 1.3 图标设计

设计一套 SVG 源文件，导出 192/512/180(maskable) 三个尺寸 PNG。

**图标概念**：在现有蓝底白箭头基础上优化——
- 主色: `#1e40af` → `#2563eb` 渐变（更鲜亮的蓝色，接近 Telegram 品牌色）
- 形状: 圆角矩形（符合 Android/iOS 自适应图标规范）
- 元素: 下载箭头 + 底部托盘，简洁现代
- Maskable 版本: 内容居中大一些，确保裁切后不被截断

生成方式：用 SVG 设计后，用 sharp/squoosh 导出 PNG，或直接用 Canvas API 生成。

#### 1.4 文件结构

```
module/
├── static/
│   ├── manifest.json
│   └── icons/
│       ├── icon-192.png
│       ├── icon-512.png
│       ├── icon-maskable-192.png
│       ├── icon-maskable-512.png
│       └── icon-180.png    (apple-touch-icon)
└── templates/
    └── index.html
```

后端 Flask 需增加 static 文件路由：
```python
_flask_app.static_folder = os.path.join(os.path.dirname(__file__), "static")
```

---

### 2. 移动端导航优化

#### 2.1 底部导航栏（移动端）

桌面端保持顶部 Tab，移动端切换为底部固定导航栏。

**设计要点**：
- `position: fixed; bottom: 0` + `padding-bottom: env(safe-area-inset-bottom)`
- 4 个 tab 等宽分布，图标 + 文字
- 选中态: 主色 + 粗体，未选中: 灰色
- Badge 数字显示在图标右上角

**HTML 结构**：
```html
<!-- 底部导航 - 仅移动端显示 -->
<nav class="fixed bottom-0 left-0 right-0 z-40 bg-white border-t border-gray-200 
            sm:hidden safe-bottom">
    <div class="flex items-center justify-around h-14">
        <button class="flex flex-col items-center gap-0.5 px-3 py-1"
                :class="activeTab === 'active' ? 'text-blue-600' : 'text-gray-400'">
            <span class="relative">
                <svg>下载箭头图标</svg>
                <span x-show="activeTasks.length" class="absolute -top-1 -right-2.5 
                      text-[10px] font-bold bg-blue-500 text-white rounded-full 
                      min-w-[16px] h-4 flex items-center justify-center px-1"
                      x-text="activeTasks.length"></span>
            </span>
            <span class="text-[10px]">下载中</span>
        </button>
        <!-- 待下载、已完成、失败 同理 -->
    </div>
</nav>
```

#### 2.2 顶部 Header 简化（移动端）

移动端去掉 Tab 栏，只保留 Header（Logo + 下载速度），Tab 切换交给底部导航。

```html
<!-- 顶部 Tab - 桌面端才显示 -->
<div class="hidden sm:block bg-white border-b border-gray-100">
    <!-- 现有 Tab 导航 -->
</div>
```

---

### 3. 卡片布局优化

#### 3.1 信息层级调整

**桌面端（≥ sm）**：保持现有横向排列，信息一行展示。

**移动端（< sm）**：改为分层展示——

```
┌─────────────────────────────────┐
│ [✓] 下载中                      │
│     filename.mp4          [⏸][🗑] │
│     12.3 MB · 2.1 MB/s · 5s    │
│     ████████████░░░░░  67.3%   │
│     群组名 · task:0704-3        │
└─────────────────────────────────┘
```

- **第一行**: 状态 badge（左）+ 操作按钮（右）
- **第二行**: 文件名（独占一行，完整显示）
- **第三行**: 大小、速度、ETA（一行，紧凑）
- **第四行**: 进度条（全宽）
- **第五行**: 群组名、任务 ID（灰色小字）

**Tailwind 响应式写法**：
```html
<!-- 卡片内容区 -->
<div class="flex-1 min-w-0">
    <!-- 桌面端：信息一行 -->
    <div class="hidden sm:flex items-center gap-2 mb-1">
        <span class="status-badge">...</span>
        <span class="chat">...</span>
        <span class="task-id">...</span>
    </div>
    <!-- 移动端：文件名独占一行 -->
    <h4 class="sm:hidden text-sm font-medium text-gray-900 truncate" 
        x-text="task.filename"></h4>
    <!-- 桌面端：文件名跟在 badge 后面 -->
    <h4 class="hidden sm:inline text-sm font-medium" 
        x-text="task.filename"></h4>
    ...
</div>
```

#### 3.2 操作按钮加大触摸区域

移动端操作按钮从小图标改为更大的触摸友好按钮：

```html
<!-- 移动端：更大的操作按钮 -->
<div class="flex sm:hidden items-center gap-2">
    <button class="w-10 h-10 rounded-xl flex items-center justify-center 
                   bg-amber-50 text-amber-600 border border-amber-200">
        <svg class="w-5 h-5">⏸</svg>
    </button>
    <button class="w-10 h-10 rounded-xl flex items-center justify-center 
                   bg-red-50 text-red-500 border border-red-200">
        <svg class="w-5 h-5">🗑</svg>
    </button>
</div>
```

#### 3.3 进度条加粗

移动端进度条从 `h-2` 增加到 `h-2.5`，更容易看到进度：

```html
<div class="w-full bg-gray-100 rounded-full overflow-hidden h-2 sm:h-2">
```

---

### 4. 批量操作栏优化

#### 4.1 移动端：底部悬浮 + 安全区

批量选择后，操作栏固定在底部（导航栏上方），避免用户滚动时丢失操作。

```html
<div x-show="selectedTasks.length > 0" x-transition
     class="fixed bottom-14 left-0 right-0 z-30 sm:static sm:bottom-auto
            bg-blue-50/95 backdrop-blur-sm border-t border-blue-200
            px-4 py-3 safe-bottom-sm">
    <div class="flex items-center justify-between max-w-7xl mx-auto">
        <span class="text-sm text-blue-700 font-medium">
            已选择 <span x-text="selectedTasks.length"></span> 项
        </span>
        <div class="flex items-center gap-2">
            <button>重试</button>
            <button>删除</button>
            <button @click="selectedTasks = []">取消</button>
        </div>
    </div>
</div>
```

---

### 5. 下拉刷新

#### 5.1 使用 CSS overscroll + JS 实现

不引入第三方库，用原生 touch 事件 + CSS 实现：

```javascript
// 在 init() 中注册
let pullStartY = 0;
let pullDistance = 0;
const PULL_THRESHOLD = 80;

document.addEventListener('touchstart', (e) => {
    if (window.scrollY === 0) pullStartY = e.touches[0].clientY;
});

document.addEventListener('touchmove', (e) => {
    if (pullStartY === 0 || window.scrollY > 0) return;
    pullDistance = e.touches[0].clientY - pullStartY;
    // 显示下拉指示器
    if (pullDistance > 10) {
        this.pullRefreshing = true;
        document.getElementById('pull-indicator').style.transform = 
            `translateY(${Math.min(pullDistance, PULL_THRESHOLD)}px)`;
    }
});

document.addEventListener('touchend', async () => {
    if (pullDistance > PULL_THRESHOLD) {
        // 触发刷新
        await Promise.all([
            this.fetchActive(),
            this.fetchPending(),
            this.fetchCompleted(false),
            this.fetchDownloadStatus(),
            this.fetchFloodWait()
        ]);
    }
    pullStartY = 0;
    pullDistance = 0;
    this.pullRefreshing = false;
});
```

```html
<!-- 下拉刷新指示器 -->
<div id="pull-indicator" 
     class="fixed top-0 left-0 right-0 z-50 flex justify-center 
            transition-transform -translate-y-12">
    <div class="mt-2 w-8 h-8 rounded-full bg-white shadow-md 
                flex items-center justify-center">
        <svg class="w-4 h-4 text-blue-500 animate-spin">...</svg>
    </div>
</div>
```

---

### 6. 全局样式优化

#### 6.1 Safe Area 适配

```css
.safe-bottom {
    padding-bottom: env(safe-area-inset-bottom);
}
.safe-bottom-sm {
    padding-bottom: calc(env(safe-area-inset-bottom) * 0.5);
}
/* main 内容区底部留空间给导航栏 */
@media (max-width: 639px) {
    main { padding-bottom: calc(4rem + env(safe-area-inset-bottom)); }
}
```

#### 6.2 文字优化

```css
/* 防止长文件名/路径截断时不自然 */
.line-clamp-2 {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}
```

移动端文件名允许显示 2 行（而非强制 truncate），让用户看到更多信息：

```html
<h4 class="sm:truncate line-clamp-2 text-sm font-medium text-gray-900" 
    x-text="task.filename"></h4>
```

#### 6.3 Toast 安全区

```html
<div class="fixed bottom-20 sm:bottom-4 right-4 z-50 
            pb-[env(safe-area-inset-bottom)]">
```

#### 6.4 暗色模式（可选，Phase 2）

当前不实现，但结构预留。TailwindCSS 的 `dark:` 前缀天然支持，未来只需：
- `tailwind.config.js` 加 `darkMode: 'media'`
- 关键元素加 `dark:bg-gray-800 dark:text-gray-100`

---

### 7. 搜索栏优化

移动端搜索栏全宽，去掉 `max-w-md` 限制：

```html
<div class="relative w-full sm:max-w-md">
```

---

### 8. 空状态优化

空状态的图标和文字在移动端稍大一些，增强视觉引导：

```html
<div class="text-center py-16 sm:py-20">
    <div class="w-20 h-20 sm:w-16 sm:h-16 bg-gray-100 rounded-full 
                flex items-center justify-center mx-auto mb-4">
        <svg class="w-10 h-10 sm:w-8 sm:h-8 text-gray-300">...</svg>
    </div>
    <h3 class="text-base sm:text-sm text-gray-500 font-medium mb-1">暂无下载任务</h3>
    <p class="text-sm sm:text-xs text-gray-400">任务开始后会在这里显示</p>
</div>
```

---

## 实施优先级

| 优先级 | 改动 | 工作量 | 影响 |
|--------|------|--------|------|
| P0 | PWA manifest + 图标 + meta 标签 | 1-2h | 可安装到主屏幕，体验飞跃 |
| P0 | 底部导航栏（移动端） | 1h | 手机操作体验质变 |
| P1 | 卡片信息层级重排（响应式） | 2h | 手机端可读性大幅提升 |
| P1 | 操作按钮触摸区域加大 | 0.5h | 减少误触 |
| P1 | 批量操作栏底部固定 | 0.5h | 手机端操作可达性 |
| P1 | Safe area 适配 | 0.5h | iPhone 刘海屏/底部适配 |
| P2 | 下拉刷新 | 1h | 手动刷新，体验加分 |
| P2 | Toast/搜索栏/空状态优化 | 1h | 细节打磨 |
| P3 | 暗色模式 | 2-3h | 锦上添花 |

**总工时估算**: P0-P1 约 6h，全做完约 9h

---

## 不做的（明确排除）

- **不做**原生 App（PWA 够用）
- **不做** Service Worker 离线缓存（下载器必须在线，离线无意义）
- **不做**桌面端改动（现有桌面布局已经很好）
- **不做**引入图标库如 Heroicons/Phosphor（现有 inline SVG 够用，加库增加加载时间）
- **不做**框架替换（TailwindCSS + Alpine.js 组合轻量高效，不引入 Vue/React）
