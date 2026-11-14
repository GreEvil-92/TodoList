/**
 * 主题配色模块（自定义强调色）
 *   1. ColorUtils         —— 颜色计算（hex/rgb、相对亮度、对比度、明暗调整）
 *   2. AccentThemeManager —— 强调色令牌的归一化、派生、注入、缓存与持久化
 *
 * 设计要点：
 *   - 默认只接管"强调层"令牌（primary/success/warning/danger/info + 4 个优先级色）；
 *     背景/文字/边框等结构层由 global.css 与 dark-mode.css 负责。
 *   - 结构层可选项：自定义模式下打开 structure.enabled 后，背景与文字色同样由本模块
 *     注入，次级背景/边框/次要文字按固定比例从这两个色派生（详见 buildStructureLines）。
 *     默认关闭，因此默认模式与深色模式的外观完全不受影响。
 *   - 派生令牌（--*-rgb / --on-* / --primary-hover / --focus-ring）由 JS 计算，
 *     不依赖 color-mix()，以兼容不同平台 WebView 内核。
 *   - 注入方式：动态 upsert <style id="accent-theme">，深浅两套一次写入，切基底无需重新注入。
 *   - 只在"自定义模式"下生效：其它模式 setActive(false) 移除注入样式，回到 global.css 出厂强调色。
 *   - 首屏防闪：本文件在 <head> 中同步加载并调用 restoreFromCache()，保证第一帧即最终外观。
 *
 * 注意：本文件在 <head> 中加载，早于 logger.js，故不依赖 logger。
 */
(function (global) {
    'use strict';

    const STORAGE_KEY = 'todolist_accent_colors';
    const STYLE_ID = 'accent-theme';
    const CONFIG_VERSION = 1;

    /** 主题模式存储键：与 js/utils/business-utils.js 的 ThemeManager 共用 */
    const THEME_MODE_KEY = 'todolist_theme';
    const MODE_DEFAULT = 'default';
    const MODE_DARK = 'dark';
    const MODE_CUSTOM = 'custom';

    /* ==================== 一、颜色计算工具 ==================== */

    const HEX6 = /^#?([0-9a-fA-F]{6})$/;
    const HEX3 = /^#?([0-9a-fA-F]{3})$/;

    function clamp255(value) {
        return Math.max(0, Math.min(255, Math.round(value)));
    }

    const ColorUtils = {
        // 归一化为 #rrggbb 小写；非法输入返回 null
        normalizeHex(value) {
            if (typeof value !== 'string') return null;
            const input = value.trim().toLowerCase();
            let matched = input.match(HEX6);
            if (matched) return '#' + matched[1];
            matched = input.match(HEX3);
            if (matched) {
                const [r, g, b] = matched[1].split('');
                return '#' + r + r + g + g + b + b;
            }
            return null;
        },

        isValidHex(value) {
            return this.normalizeHex(value) !== null;
        },

        /** '#007bff' -> { r: 0, g: 123, b: 255 } */
        hexToRgb(value) {
            const hex = this.normalizeHex(value);
            if (!hex) return null;
            return {
                r: parseInt(hex.slice(1, 3), 16),
                g: parseInt(hex.slice(3, 5), 16),
                b: parseInt(hex.slice(5, 7), 16)
            };
        },

        /** '#007bff' -> '0, 123, 255'（写入 --xxx-rgb 令牌） */
        hexToRgbTuple(value) {
            const rgb = this.hexToRgb(value);
            return rgb ? `${rgb.r}, ${rgb.g}, ${rgb.b}` : null;
        },

        /** { r, g, b } + alpha -> 'rgba(...)' / 'rgb(...)' */
        rgbToCss(rgb, alpha = 1) {
            if (!rgb) return null;
            if (alpha >= 1) return `rgb(${rgb.r}, ${rgb.g}, ${rgb.b})`;
            return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
        },

        /** WCAG 相对亮度 */
        relativeLuminance(rgb) {
            if (!rgb) return 0;
            const [r, g, b] = [rgb.r, rgb.g, rgb.b].map((channel) => {
                const s = channel / 255;
                return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
            });
            return 0.2126 * r + 0.7152 * g + 0.0722 * b;
        },

        /** WCAG 对比度（1 ~ 21） */
        contrastRatio(rgbA, rgbB) {
            if (!rgbA || !rgbB) return 1;
            const la = this.relativeLuminance(rgbA);
            const lb = this.relativeLuminance(rgbB);
            const lighter = Math.max(la, lb);
            const darker = Math.min(la, lb);
            return (lighter + 0.05) / (darker + 0.05);
        },

        // 在有色底上选可读前景色（--on-* 令牌）
        pickReadableText(bgHex, darkText = '#212529', lightText = '#ffffff') {
            const bg = this.hexToRgb(bgHex);
            const dark = this.hexToRgb(darkText);
            const light = this.hexToRgb(lightText);
            if (!bg) return lightText;
            return this.contrastRatio(bg, dark) >= this.contrastRatio(bg, light) ? darkText : lightText;
        },

        /** 变暗：各通道乘以 (1 - ratio) */
        darken(value, ratio = 0.2) {
            const rgb = this.hexToRgb(value);
            if (!rgb) return value;
            const factor = 1 - ratio;
            return this._toHex({
                r: clamp255(rgb.r * factor),
                g: clamp255(rgb.g * factor),
                b: clamp255(rgb.b * factor)
            });
        },

        /** 变亮：各通道向 255 靠拢 */
        lighten(value, ratio = 0.2) {
            const rgb = this.hexToRgb(value);
            if (!rgb) return value;
            return this._toHex({
                r: clamp255(rgb.r + (255 - rgb.r) * ratio),
                g: clamp255(rgb.g + (255 - rgb.g) * ratio),
                b: clamp255(rgb.b + (255 - rgb.b) * ratio)
            });
        },

        /** 两色按比例线性混合：ratio=0 取 from，ratio=1 取 to（结构层派生色用） */
        mix(from, to, ratio = 0.5) {
            const source = this.hexToRgb(from);
            const target = this.hexToRgb(to);
            if (!source || !target) return from;
            return this._toHex({
                r: clamp255(source.r + (target.r - source.r) * ratio),
                g: clamp255(source.g + (target.g - source.g) * ratio),
                b: clamp255(source.b + (target.b - source.b) * ratio)
            });
        },

        _toHex(rgb) {
            const part = (n) => n.toString(16).padStart(2, '0');
            return `#${part(rgb.r)}${part(rgb.g)}${part(rgb.b)}`;
        }
    };

    /* ==================== 二、令牌定义 ==================== */

    // linked：优先级联动时该令牌由语义色派生，不可单独编辑；semantic 缺省则用中性灰
    const TOKENS = [
        { key: 'primary', label: '主体颜色', cssVar: '--primary-color', rgbVar: '--primary-rgb', onVar: '--on-primary' },
        { key: 'success', label: '成功颜色', cssVar: '--success-color', rgbVar: '--success-rgb', onVar: '--on-success' },
        { key: 'warning', label: '警告颜色', cssVar: '--warning-color', rgbVar: '--warning-rgb', onVar: '--on-warning' },
        { key: 'danger', label: '危险颜色', cssVar: '--danger-color', rgbVar: '--danger-rgb', onVar: '--on-danger' },
        { key: 'info', label: '信息颜色', cssVar: '--info-color', rgbVar: '--info-rgb', onVar: '--on-info' },
        { key: 'priorityHigh', label: '高优先级', cssVar: '--priority-high', onVar: '--on-priority-high', semantic: 'danger', linked: true },
        { key: 'priorityMedium', label: '中优先级', cssVar: '--priority-medium', onVar: '--on-priority-medium', semantic: 'warning', linked: true },
        { key: 'priorityLow', label: '低优先级', cssVar: '--priority-low', onVar: '--on-priority-low', semantic: 'success', linked: true },
        { key: 'priorityNone', label: '无优先级', cssVar: '--priority-none', onVar: '--on-priority-none', linked: true }
    ];

    /** 优先级"无"联动时使用的中性灰（结构色，不在可自定义范围内） */
    const NEUTRAL_PRIORITY_NONE = '#6c757d';

    /** 出厂配色：浅色与深色保持一致，避免默认外观发生变化 */
    const DEFAULT_PALETTE = {
        primary: '#007bff',
        success: '#28a745',
        warning: '#ffc107',
        danger: '#dc3545',
        info: '#17a2b8',
        priorityHigh: '#dc3545',
        priorityMedium: '#ffc107',
        priorityLow: '#28a745',
        priorityNone: NEUTRAL_PRIORITY_NONE
    };

    /* ==================== 三、结构层（背景 / 文字）定义 ==================== */

    /**
     * 结构层可编辑项。出厂值必须与 css 保持一致：
     *   浅色取 global.css :root，深色取 dark-mode.css [data-theme="dark"]。
     */
    const STRUCTURE_TOKENS = [
        { key: 'background', label: '背景配色' },
        { key: 'text', label: '字体配色' }
    ];

    const STRUCTURE_DEFAULTS = {
        light: { background: '#ffffff', text: '#212529' },
        dark: { background: '#1a1a1a', text: '#ffffff' }
    };

    /* ==================== 四、配置归一化与派生 ==================== */

    function pickPalette(source, mode) {
        const modeSource = source && typeof source[mode] === 'object' && source[mode] ? source[mode] : {};
        const palette = {};
        TOKENS.forEach((token) => {
            palette[token.key] = ColorUtils.normalizeHex(modeSource[token.key]) || DEFAULT_PALETTE[token.key];
        });
        return palette;
    }

    // 整理任意外部输入（后端/缓存/UI）为合法配置，非法值回退出厂值
    function normalizeConfig(raw) {
        const source = raw && typeof raw === 'object' ? raw : {};
        return {
            version: CONFIG_VERSION,
            // 默认联动，与出厂行为一致
            linkPriority: source.linkPriority !== false,
            // 自定义模式所依赖的基底主题：决定 --bg-* / --text-* 等结构色
            baseTheme: source.baseTheme === 'dark' ? 'dark' : 'light',
            light: pickPalette(source, 'light'),
            dark: pickPalette(source, 'dark'),
            structure: {
                light: pickStructure(source.structure, 'light'),
                dark: pickStructure(source.structure, 'dark')
            }
        };
    }

    /**
     * 结构层配置归一化。
     * enabled 缺省为 false：老配置（没有 structure 字段）与新出厂值行为一致，
     * 即"跟随主题"，不会因为升级就改掉界面底色。
     */
    function pickStructure(source, mode) {
        const fallback = STRUCTURE_DEFAULTS[mode];
        const modeSource = source && typeof source[mode] === 'object' && source[mode] ? source[mode] : {};
        return {
            enabled: modeSource.enabled === true,
            background: ColorUtils.normalizeHex(modeSource.background) || fallback.background,
            text: ColorUtils.normalizeHex(modeSource.text) || fallback.text
        };
    }

    // 解析某模式下最终生效的配色（含优先级联动）
    function resolvePalette(config, mode) {
        const palette = Object.assign({}, DEFAULT_PALETTE, config[mode] || {});
        if (config.linkPriority !== false) {
            TOKENS.forEach((token) => {
                if (!token.linked) return;
                palette[token.key] = token.semantic
                    ? palette[token.semantic]
                    : NEUTRAL_PRIORITY_NONE;
            });
        }
        return palette;
    }

    /* ==================== 五、CSS 生成与注入 ==================== */

    /**
     * 由用户选的「背景色 + 文字色」派生整套结构层令牌。
     *
     * 职责分离：背景系令牌（--bg-secondary/--bg-tertiary/--bg-darker/--border-*）
     * 只由背景色派生，文字系令牌（--text-secondary/--text-muted）只由文字色派生，
     * 两者互不干扰。旧实现把「文字色」当作背景系的混合锚点，导致：
     *   - 改文字色会连带改变次级背景与边框（观感上"整个界面都变了"）；
     *   - 改背景色时次级背景几乎不动，只有 --bg-primary 明显变化，
     *     观感上像是在改"最上层图层"而不是背景。
     *
     * 派生锚点改用纯黑/白并按明暗方向选择：浅色底向黑加深一档、深色底向白提亮一档，
     * 因此同一组比例对任意底色都成立。出厂默认值（浅 #ffffff/#212529、
     * 深 #1a1a1a/#ffffff）派生结果与 global.css / dark-mode.css 的出厂令牌量级一致。
     *
     * 刻意不动 --bg-dark 与 --text-white：toast / tooltip 固定用白字（components.css），
     * 跟着背景色走会出现白字浅底。
     */
    function buildStructureLines(structure) {
        const bg = structure.background;
        const text = structure.text;

        const bgRgb = ColorUtils.hexToRgb(bg);
        const textRgb = ColorUtils.hexToRgb(text);
        // 底色比文字亮 → 浅色基底（向黑派生）；否则按深色基底（向白派生）
        const bgIsLight = ColorUtils.relativeLuminance(bgRgb)
            >= ColorUtils.relativeLuminance(textRgb);
        const bgAnchor = bgIsLight ? '#000000' : '#ffffff';
        const textAnchor = bgIsLight ? '#ffffff' : '#000000';
        const mixBg = (ratio) => ColorUtils.mix(bg, bgAnchor, ratio);
        const mixText = (ratio) => ColorUtils.mix(text, textAnchor, ratio);

        return [
            `    --bg-primary: ${bg};`,
            `    --bg-secondary: ${mixBg(0.04)};`,
            `    --bg-tertiary: ${mixBg(0.11)};`,
            // 出厂深色 --bg-darker(#212529) 相对 #1a1a1a 只向白偏 ~3%，
            // 比例大了会把深色模式的弹窗底色明显冲淡
            `    --bg-darker: ${mixBg(0.03)};`,
            `    --border-color: ${mixBg(0.16)};`,
            `    --border-light: ${mixBg(0.07)};`,
            `    --text-primary: ${text};`,
            `    --text-color: ${text};`,
            `    --text-secondary: ${mixText(0.35)};`,
            `    --text-muted: ${mixText(0.55)};`
        ];
    }

    function buildCssBlock(selector, palette, structure) {
        const lines = [];

        TOKENS.forEach((token) => {
            const hex = palette[token.key];
            lines.push(`    ${token.cssVar}: ${hex};`);
            if (token.rgbVar) {
                lines.push(`    ${token.rgbVar}: ${ColorUtils.hexToRgbTuple(hex)};`);
            }
            lines.push(`    ${token.onVar}: ${ColorUtils.pickReadableText(hex)};`);
        });

        // 派生令牌
        const primaryRgb = ColorUtils.hexToRgb(palette.primary);
        // darken 0.30 时 #007bff -> #0056b3，与出厂值一致
        lines.push(`    --primary-hover: ${ColorUtils.darken(palette.primary, 0.3)};`);
        lines.push(`    --focus-ring: rgba(${primaryRgb.r}, ${primaryRgb.g}, ${primaryRgb.b}, 0.25);`);

        // 结构层：仅在用户打开「自定义背景与文字」时注入，否则完全交回出厂 CSS
        if (structure && structure.enabled) {
            lines.push(...buildStructureLines(structure));
        }

        return `${selector} {\n${lines.join('\n')}\n}`;
    }

    function buildCss(config) {
        const structure = config.structure || {};
        return [
            '/* 由 AccentThemeManager 注入（js/utils/theme-colors.js），请勿手改；出厂值见 css/global.css */',
            buildCssBlock(':root', resolvePalette(config, 'light'), structure.light),
            buildCssBlock('[data-theme="dark"]', resolvePalette(config, 'dark'), structure.dark)
        ].join('\n\n');
    }

    function inject(css) {
        let styleEl = document.getElementById(STYLE_ID);
        if (!styleEl) {
            styleEl = document.createElement('style');
            styleEl.id = STYLE_ID;
            (document.head || document.documentElement).appendChild(styleEl);
        }
        styleEl.textContent = css;
    }

    /** 移除注入样式，回到 global.css 的出厂强调色 */
    function removeStyle() {
        const styleEl = document.getElementById(STYLE_ID);
        if (styleEl && styleEl.parentNode) styleEl.parentNode.removeChild(styleEl);
    }

    /** 读取当前主题模式（'default' | 'dark' | 'custom'），兼容历史的 'light' */
    function readStoredMode() {
        let raw = null;
        try {
            raw = global.localStorage.getItem(THEME_MODE_KEY);
        } catch (error) {
            raw = null;
        }
        if (raw === MODE_DARK || raw === MODE_CUSTOM) return raw;
        return MODE_DEFAULT;
    }

    /* ==================== 六、本地缓存 ==================== */

    function readCache() {
        try {
            const raw = global.localStorage.getItem(STORAGE_KEY);
            if (!raw) return null;
            return normalizeConfig(JSON.parse(raw));
        } catch (error) {
            return null;
        }
    }

    function writeCache(config) {
        try {
            global.localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
        } catch (error) {
            /* 隐私模式等场景下写缓存失败不影响功能 */
        }
    }

    function currentMode() {
        return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
    }

    /* ==================== 七、对外接口 ==================== */

    const AccentThemeManager = {
        STORAGE_KEY,
        STYLE_ID,
        TOKENS,
        DEFAULT_PALETTE,
        STRUCTURE_TOKENS,
        STRUCTURE_DEFAULTS,
        ColorUtils,
        MODES: { DEFAULT: MODE_DEFAULT, DARK: MODE_DARK, CUSTOM: MODE_CUSTOM },

        /** 出厂配置（深拷贝） */
        getDefaultConfig() {
            return normalizeConfig(null);
        },

        normalizeConfig,

        /** 当前生效配置（本地缓存优先，无缓存则为出厂配置） */
        getConfig() {
            return readCache() || normalizeConfig(null);
        },

        /** 指定基底下的最终配色（已处理优先级联动） */
        getPalette(mode) {
            return resolvePalette(this.getConfig(), mode || currentMode());
        },

        /** 当前基底 5 个语义色的 hex 数组（供分类默认色等场景使用） */
        getAccentHexPalette() {
            const palette = this.getPalette(currentMode());
            return [palette.primary, palette.success, palette.danger, palette.warning, palette.info];
        },

        /** 当前是否已应用自定义配色 */
        isActive() {
            return !!this._active;
        },

        // 首屏同步恢复（在 <head> 内联脚本中调用，避免深浅色/配色闪变）；仅自定义模式注入
        restoreFromCache() {
            const mode = readStoredMode();
            if (mode !== MODE_CUSTOM) {
                document.documentElement.setAttribute(
                    'data-theme', mode === MODE_DARK ? 'dark' : 'light');
                return false;
            }

            const config = readCache() || normalizeConfig(null);
            this._active = true;
            document.documentElement.setAttribute('data-theme', config.baseTheme);
            inject(buildCss(config));
            return true;
        },

        // true → 注入样式并按 baseTheme 设 data-theme；false → 移除样式，基底交回 ThemeManager
        setActive(active) {
            this._active = !!active;
            this._snapshot = null;

            if (!this._active) {
                removeStyle();
                return;
            }

            const config = this.getConfig();
            inject(buildCss(config));
            document.documentElement.setAttribute('data-theme', config.baseTheme);
        },

        // 持久化成功后调用，同时结束预览态。非自定义模式只更新缓存，避免强调色被意外改掉
        apply(rawConfig) {
            const config = normalizeConfig(rawConfig);
            writeCache(config);
            this._snapshot = null;

            if (this._active) {
                inject(buildCss(config));
                document.documentElement.setAttribute('data-theme', config.baseTheme);
            }
            return config;
        },

        /** 实时预览：只改样式，不落缓存，可用 cancelPreview 回滚 */
        preview(rawConfig) {
            if (!this._active) return;
            if (!this._snapshot) this._snapshot = this.getConfig();

            const config = normalizeConfig(rawConfig);
            inject(buildCss(config));
            document.documentElement.setAttribute('data-theme', config.baseTheme);
        },

        /** 放弃预览，回到上次保存的配色 */
        cancelPreview() {
            this._snapshot = null;
            if (!this._active) return;

            const config = this.getConfig();
            inject(buildCss(config));
            document.documentElement.setAttribute('data-theme', config.baseTheme);
        },

        // 启动期与后端对齐：仅自定义模式注入；后端无配置时保留本地缓存，避免配色闪回默认值
        async reconcile() {
            if (!global.Utils || typeof global.Utils.apiCall !== 'function') return;
            await global.Utils.apiCall({
                apiMethod: 'get_config',
                apiArgs: ['custom_accent_colors'],
                onSuccess: (response) => {
                    const remote = response && response.data ? response.data.custom_accent_colors : null;
                    if (!remote) return;

                    const normalized = normalizeConfig(remote);
                    const cached = readCache();
                    if (cached && JSON.stringify(cached) === JSON.stringify(normalized)) return;

                    if (this._active) {
                        this.apply(normalized);
                    } else {
                        writeCache(normalized);
                    }
                }
            });
        }
    };

    global.AccentThemeManager = AccentThemeManager;
})(window);
