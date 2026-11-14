/**
 * 设置中心：桌面显示插件选项（mixin，挂载到 SettingsUIManager 原型）
 *
 * 两个设置项，改动即时保存（与窗口置顶同一套交互）：
 * - 显示任务范围：优先级多选（高/中/低/无），决定插件统计与倒计时看哪些任务；
 * - 倒计时区间：单选（今日/明日/本周内/本月内），决定倒计时取哪个区间内
 *   最近的截止日期；区间内没有任务时插件显示「无事发生」。
 *
 * 存储走 CONFIG_REGISTRY 的 widget_priorities / widget_countdown_range 两个键；
 * 插件端每 2 秒刷新时会重读设置，保存后无需重启即可生效。
 */

Object.assign(SettingsUIManager.prototype, {

    initWidgetSettingsDOM() {
        this.widgetPriorityGroup = document.getElementById('widget-priority-group');
        this.widgetRangeGroup = document.getElementById('widget-range-group');
        this.widgetSettingsHint = document.getElementById('widget-settings-hint');
    },

    bindWidgetSettingsEvents() {
        this.widgetPriorityGroup?.addEventListener('change', () => this.handleWidgetSettingsChange());
        this.widgetRangeGroup?.addEventListener('change', () => this.handleWidgetSettingsChange());
    },

    /** 把选中项收集成取数参数：优先级数组 + 区间 key */
    collectWidgetSettings() {
        const priorities = Array.from(
            this.widgetPriorityGroup?.querySelectorAll('input[type="checkbox"]:checked') || []
        ).map(input => input.value);
        const range = this.widgetRangeGroup?.querySelector('input[type="radio"]:checked')?.value || 'month';
        return { priorities, range };
    },

    /** 刷新设置中心入口右侧的配置摘要（如「全部优先级 · 本月内」） */
    updateWidgetSettingsHint(priorities, range) {
        if (!this.widgetSettingsHint) return;
        const t = (key, fallback) => window.languageManager.getText(key, fallback);

        let scope;
        if (!priorities.length) {
            scope = t('widgetHintNoPriority', '未选择优先级');
        } else if (priorities.length === 4) {
            scope = t('widgetHintAllPriority', '全部优先级');
        } else {
            const names = { high: 'priorityHigh', medium: 'priorityMedium', low: 'priorityLow', none: 'priorityNone' };
            scope = priorities.map(p => t(names[p], p)).join('+');
        }
        const ranges = { today: 'widgetRangeToday', tomorrow: 'widgetRangeTomorrow', week: 'widgetRangeWeek', month: 'widgetRangeMonth' };
        this.widgetSettingsHint.textContent = `${scope} · ${t(ranges[range] || 'widgetRangeMonth', range)}`;
    },

    /** 打开设置弹窗时回显当前配置（数据库为准，接口失败时保持现状不闪变） */
    async updateWidgetSettingsState() {
        if (!this.widgetPriorityGroup && !this.widgetRangeGroup) return;

        await Utils.apiCall({
            apiMethod: 'get_config',
            apiArgs: ['widget_priorities,widget_countdown_range'],
            onSuccess: (response) => {
                const priorities = Array.isArray(response.data.widget_priorities)
                    ? response.data.widget_priorities : ['high'];
                this.widgetPriorityGroup?.querySelectorAll('input[type="checkbox"]').forEach(input => {
                    input.checked = priorities.includes(input.value);
                });
                const range = response.data.widget_countdown_range || 'month';
                this.widgetRangeGroup?.querySelectorAll('input[type="radio"]').forEach(input => {
                    input.checked = input.value === range;
                });
                this.updateWidgetSettingsHint(priorities, range);
            }
        });
    },

    /** 任一选项变化：立即写库并轻提示 */
    async handleWidgetSettingsChange() {
        const { priorities, range } = this.collectWidgetSettings();
        let failed = false;
        const onError = (error) => {
            failed = true;
            Utils.showToast(
                `${this.t('widgetSettingsFailed', '桌面插件设置保存失败')}: ${error.message}`, 'error');
        };

        await Utils.apiCall({
            apiMethod: 'set_config',
            apiArgs: ['widget_priorities', priorities],
            onError
        });
        await Utils.apiCall({
            apiMethod: 'set_config',
            apiArgs: ['widget_countdown_range', range],
            onError
        });
        // 两次写入都成功才提示"已保存"，避免失败时弹出矛盾的双重提示
        if (!failed) {
            Utils.showToast(this.t('widgetSettingsSaved', '桌面插件设置已保存'), 'success');
        }
        this.updateWidgetSettingsHint(priorities, range);
    }
});
