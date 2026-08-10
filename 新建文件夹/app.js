const roles = {
  scenic: {
    label: "景区",
    intro: "官方内容、活动日历、门票和套票转化。",
    title: "景区",
    detailTitle: "景区侧的重点是内容可信度和即时转化。",
    bullets: [
      "内容供给：官方图文、活动、导览、路线。",
      "转化抓手：门票、套票、季节活动、预约。",
      "关键指标：曝光、收藏、咨询、购票。",
    ],
    accent: "#167a7f",
    accentSoft: "rgba(22, 122, 127, 0.12)",
  },
  stay: {
    label: "民宿",
    intro: "房态、套餐、周末微度假和复购。",
    title: "民宿",
    detailTitle: "民宿侧更适合做场景化套餐和私域承接。",
    bullets: [
      "内容供给：房型、庭院、早餐、周边体验。",
      "转化抓手：房态、套餐、连住优惠、私信咨询。",
      "关键指标：收藏、咨询、到店、复购。",
    ],
    accent: "#d86a4d",
    accentSoft: "rgba(216, 106, 77, 0.12)",
  },
  local: {
    label: "本地体验",
    intro: "路线、预约、活动报名和即时服务。",
    title: "本地体验",
    detailTitle: "本地体验适合把“看见”直接转成“报名”。",
    bullets: [
      "内容供给：路线、时长、价格、服务说明。",
      "转化抓手：预约、拼团、活动报名、客服。",
      "关键指标：报名率、转化率、评价分。",
    ],
    accent: "#c79a43",
    accentSoft: "rgba(199, 154, 67, 0.14)",
  },
  creator: {
    label: "达人机构",
    intro: "内容分发、品牌合作和线索管理。",
    title: "达人机构",
    detailTitle: "达人机构侧重点在内容管理和商业合作效率。",
    bullets: [
      "内容供给：游记、视频、榜单、专题。",
      "转化抓手：品牌合作、引流、达人联动。",
      "关键指标：阅读、互动、私信、合作单。",
    ],
    accent: "#3e6db5",
    accentSoft: "rgba(62, 109, 181, 0.12)",
  },
};

const stories = [
  {
    title: "腾冲温泉与火山周末",
    meta: "3 天 2 晚 · 民宿 + 温泉",
    theme: "自然",
    role: "民宿",
    tag: "慢旅行",
    image: "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=900&q=80",
  },
  {
    title: "厦门海岸骑行线",
    meta: "2 天 1 晚 · 城市海岸",
    theme: "城市",
    role: "本地体验",
    tag: "周末",
    image: "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?auto=format&fit=crop&w=900&q=80",
  },
  {
    title: "亲子森林营地",
    meta: "2 天 1 晚 · 自然教育",
    theme: "亲子",
    role: "景区",
    tag: "亲子",
    image: "https://images.unsplash.com/photo-1517824806704-9040b037703b?auto=format&fit=crop&w=900&q=80",
  },
  {
    title: "顺德寻味小城",
    meta: "4 站 · 美食地图",
    theme: "美食",
    role: "达人机构",
    tag: "美食",
    image: "https://images.unsplash.com/photo-1555396273-367ea4eb4db5?auto=format&fit=crop&w=900&q=80",
  },
];

const views = {
  brief: {
    title: "方案概览",
    lead: "优先做一个内容种草平台，再把收藏、规划、咨询和轻预订接成闭环。",
    items: [
      ["产品核心", "内容入口 + 行程工具 + 轻交易"],
      ["商业模型", "B 端入驻 / 推广 / 佣金 / 工具会员"],
      ["目标用户", "C 端旅行决策人群 + B 端供给方"],
      ["核心原则", "真实内容、快速决策、低摩擦转化"],
    ],
  },
  structure: {
    title: "信息架构",
    lead: "首页负责拉新，目的地页负责聚合，详情页负责决策，行程页负责执行。",
    items: [
      ["首页", "主题榜单、目的地、达人推荐、快捷入口"],
      ["目的地页", "地图、内容流、标签筛选、商家聚合"],
      ["内容详情", "图文/视频、路线、收藏、咨询、预订"],
      ["行程页", "时间线、预算、提醒、协同编辑"],
    ],
  },
  visual: {
    title: "视觉系统",
    lead: "整体偏清爽、信息密度高，像一个能长期使用的工作台，而不是宣传页。",
    items: [
      ["配色", "米白底 + 墨蓝字 + 海湾青 + 珊瑚橘 + 琥珀金"],
      ["组件", "6px 圆角、细边框、低阴影、可扫读状态条"],
      ["图形", "地图轨迹、路线节点、内容标签、时间线"],
      ["排版", "大标题强层级，列表和表格保持紧凑对齐"],
    ],
  },
  tech: {
    title: "技术建议",
    lead: "前台和内容层分开，地图、搜索、账号和埋点单独拆服务，后续更好扩展。",
    items: [
      ["前台", "Next.js / React + SSR"],
      ["内容层", "Headless CMS + Markdown / 富文本"],
      ["能力层", "搜索、地图、登录、消息、分析"],
      ["数据层", "行为埋点、内容审核、商家看板"],
    ],
  },
};

const roleSwitcher = document.getElementById("roleSwitcher");
const roleTitle = document.getElementById("roleTitle");
const roleIntro = document.getElementById("roleIntro");
const roleDetail = document.getElementById("roleDetail");
const viewButtons = document.querySelectorAll("[data-view]");
const moodFilters = document.getElementById("moodFilters");
const storyGrid = document.getElementById("storyGrid");
const travelSearch = document.getElementById("travelSearch");
let activeTheme = "全部";

function renderRole(roleKey) {
  const role = roles[roleKey];
  if (!role) return;

  document.documentElement.style.setProperty("--accent", role.accent);
  document.documentElement.style.setProperty("--accent-soft", role.accentSoft);
  roleTitle.textContent = role.title;
  roleIntro.textContent = role.intro;
  roleDetail.innerHTML = `
    <h3>${role.detailTitle}</h3>
    <ul>
      ${role.bullets.map((item) => `<li>${item}</li>`).join("")}
    </ul>
  `;

  roleSwitcher.querySelectorAll("[data-role]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.role === roleKey);
  });
}

function renderView(viewKey) {
  const view = views[viewKey] || views.brief;

  const panel = document.getElementById("dynamicPanel");
  panel.innerHTML = `
    <div class="section-head">
      <p class="section-kicker">当前视图</p>
      <h2>${view.title}</h2>
    </div>
    <p class="lede compact">${view.lead}</p>
    <div class="detail-grid">
      ${view.items
        .map(
          ([label, value]) => `
            <div class="detail-item">
              <strong>${label}</strong>
              <span>${value}</span>
            </div>
          `,
        )
        .join("")}
    </div>
  `;

  viewButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === viewKey);
  });
}

function renderStoryFilters() {
  const themes = ["全部", ...new Set(stories.map((story) => story.theme))];
  moodFilters.innerHTML = themes
    .map(
      (theme) => `
        <button class="filter-chip ${theme === activeTheme ? "is-active" : ""}" type="button" data-theme="${theme}">
          ${theme}
        </button>
      `,
    )
    .join("");
}

function renderStories() {
  const query = travelSearch.value.trim().toLowerCase();
  const visible = stories.filter((story) => {
    const matchesTheme = activeTheme === "全部" || story.theme === activeTheme;
    const haystack = `${story.title} ${story.meta} ${story.theme} ${story.role} ${story.tag}`.toLowerCase();
    return matchesTheme && haystack.includes(query);
  });

  storyGrid.innerHTML =
    visible.length > 0
      ? visible
          .map(
            (story) => `
              <article class="story-card">
                <img src="${story.image}" alt="${story.title}" loading="lazy" />
                <div class="story-card-body">
                  <h3>${story.title}</h3>
                  <p>${story.meta}</p>
                  <div class="story-meta">
                    <span>${story.theme}</span>
                    <span>${story.role}</span>
                    <span>${story.tag}</span>
                  </div>
                </div>
              </article>
            `,
          )
          .join("")
      : `<div class="empty-state">没有匹配的灵感内容，可以换一个主题或关键词。</div>`;
}

function bootstrap() {
  roleSwitcher.innerHTML = Object.entries(roles)
    .map(
      ([key, role]) => `
        <button class="role-chip ${key === "scenic" ? "is-active" : ""}" type="button" data-role="${key}">
          ${role.label}
        </button>
      `,
    )
    .join("");

  const heroVisual = document.querySelector(".hero-visual");
  const detailPanel = document.createElement("div");
  detailPanel.className = "detail-panel";
  detailPanel.id = "dynamicPanel";
  heroVisual.appendChild(detailPanel);

  roleSwitcher.addEventListener("click", (event) => {
    const button = event.target.closest("[data-role]");
    if (!button) return;
    renderRole(button.dataset.role);
  });

  viewButtons.forEach((button) => {
    button.addEventListener("click", () => renderView(button.dataset.view));
  });

  moodFilters.addEventListener("click", (event) => {
    const button = event.target.closest("[data-theme]");
    if (!button) return;
    activeTheme = button.dataset.theme;
    renderStoryFilters();
    renderStories();
  });

  travelSearch.addEventListener("input", renderStories);

  renderRole("scenic");
  renderView("brief");
  renderStoryFilters();
  renderStories();
}

bootstrap();
