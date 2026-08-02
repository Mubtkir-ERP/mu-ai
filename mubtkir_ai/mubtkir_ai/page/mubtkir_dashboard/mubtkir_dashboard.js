frappe.pages['mubtkir-dashboard'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('لوحة مُبتكِر'),
		single_column: true,
	});
	MubtkirDash.inject_styles();
	const dash = new MubtkirDash(page);
	page.set_primary_action(__('تحديث'), () => dash.load(), 'refresh');
	dash.load();
};

class MubtkirDash {
	constructor(page) {
		this.page = page;
		this.$body = $(page.body);
	}

	load() {
		this.$body.html('<div class="mbkd-loading">جارِ التحميل…</div>');
		frappe.call({
			method: 'mubtkir_ai.api.dashboard.get_data',
			callback: (r) => this.render(r.message || {}),
			error: () => this.$body.html('<div class="mbkd-loading">تعذّر تحميل البيانات</div>'),
		});
	}

	fmt(n) {
		return frappe.format(n || 0, { fieldtype: 'Int' });
	}

	render(d) {
		const t = d.today || {}, m = d.month || {}, tot = d.total || {}, h = d.health || {};
		const cachePct = m.tokens ? Math.round((m.cached / m.tokens) * 100) : 0;

		this.$body.html(`
		<div class="mbkd" dir="rtl" lang="ar">
			<div class="mbkd-status ${h.configured ? 'ok' : 'warn'}">
				${h.configured
					? `● البوابة مهيّأة — ${frappe.utils.escape_html(h.provider || '')} / ${frappe.utils.escape_html(h.model || '')}`
					: '● البوابة غير مهيّأة — أضف مفتاح API في الإعدادات'}
			</div>

			<div class="mbkd-tiles">
				${this.tile('نداءات اليوم', this.fmt(t.calls), 'توكن: ' + this.fmt(t.tokens))}
				${this.tile('توكن هذا الشهر', this.fmt(m.tokens), 'نداءات: ' + this.fmt(m.calls))}
				${this.tile('تكلفة الشهر ($)', (m.cost || 0).toFixed(4), 'الإجمالي الكلي: $' + (tot.cost || 0).toFixed(4))}
				${this.tile('كاش الشهر', cachePct + '%', this.fmt(m.cached) + ' توكن موفَّر')}
				${this.tile('قاعدة المعرفة', this.fmt(h.kb_sources), 'مقاطع مفهرسة: ' + this.fmt(h.kb_chunks))}
			</div>

			<div class="mbkd-grid">
				<div class="mbkd-card">
					<div class="mbkd-card-title">استهلاك التوكن — آخر 14 يومًا</div>
					<div class="mbkd-chart" id="mbkd-chart"></div>
				</div>
				<div class="mbkd-card">
					<div class="mbkd-card-title">روابط سريعة</div>
					<div class="mbkd-links">
						<a href="/app/mubtkir-chat">💬 المساعد الذكي</a>
						<a href="/app/ai-knowledge-source">📚 قاعدة المعرفة</a>
						<a href="/app/ai-session">🗂️ جلسات المحادثة</a>
						<a href="/app/ai-training-candidate">🎓 مرشّحات التدريب</a>
						<a href="/app/ai-usage-log">📈 سجل الاستهلاك</a>
						<a href="/app/mubtkir-ai-settings">⚙️ الإعدادات</a>
					</div>
				</div>
			</div>

			<div class="mbkd-card">
				<div class="mbkd-card-title">آخر النداءات</div>
				<div class="mbkd-tablewrap">
					<table class="mbkd-table">
						<thead><tr>
							<th>الوقت</th><th>المستخدم</th><th>الميزة</th><th>النموذج</th>
							<th>التوكن</th><th>كاش</th><th>$</th>
						</tr></thead>
						<tbody>
							${(d.recent || []).map((r) => `
								<tr>
									<td class="mbkd-num">${frappe.utils.escape_html(r.creation || '')}</td>
									<td>${frappe.utils.escape_html(r.user || '')}</td>
									<td>${frappe.utils.escape_html(r.feature || '')}</td>
									<td>${frappe.utils.escape_html(r.model || '')}</td>
									<td class="mbkd-num">${this.fmt(r.total_tokens)}</td>
									<td class="mbkd-num">${this.fmt(r.cached_tokens)}</td>
									<td class="mbkd-num">${(r.cost_usd || 0).toFixed(5)}</td>
								</tr>`).join('') || '<tr><td colspan="7" class="mbkd-empty">لا نداءات بعد — جرّب المساعد الذكي</td></tr>'}
						</tbody>
					</table>
				</div>
			</div>
		</div>`);

		this.render_chart(d.series || []);
	}

	tile(label, value, sub) {
		return `<div class="mbkd-tile">
			<div class="mbkd-tile-v">${value}</div>
			<div class="mbkd-tile-l">${label}</div>
			<div class="mbkd-tile-s">${sub || ''}</div>
		</div>`;
	}

	render_chart(series) {
		const max = Math.max(1, ...series.map((s) => s.tokens));
		const $c = this.$body.find('#mbkd-chart');
		$c.html(series.map((s) => {
			const h = Math.max(3, Math.round((s.tokens / max) * 100));
			const day = s.date.slice(5); // MM-DD
			return `<div class="mbkd-bar-wrap" title="${s.date}: ${s.tokens} توكن">
				<div class="mbkd-bar" style="height:${h}%"></div>
				<div class="mbkd-bar-l">${day}</div>
			</div>`;
		}).join(''));
	}
}

MubtkirDash.inject_styles = function () {
	if (document.getElementById('mbkd-styles')) return;
	const css = `
	.mbkd{max-width:1100px;margin:0 auto;display:flex;flex-direction:column;gap:14px;padding-bottom:40px}
	.mbkd-loading{text-align:center;padding:60px;color:var(--text-muted,#6b7280)}
	.mbkd-status{padding:9px 14px;border-radius:10px;font-size:13px;font-weight:600}
	.mbkd-status.ok{background:#DDEBE1;color:#2C6E49}
	.mbkd-status.warn{background:#F6ECD6;color:#9A6A12}
	.mbkd-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
	.mbkd-tile{background:var(--card-bg,#fff);border:1px solid var(--border-color,#e2e8f0);
		border-radius:12px;padding:16px}
	.mbkd-tile-v{font-size:26px;font-weight:800;color:#0E7C86;font-variant-numeric:tabular-nums;line-height:1.1}
	.mbkd-tile-l{font-size:13px;color:var(--text-color,#16211f);margin-top:7px;font-weight:600}
	.mbkd-tile-s{font-size:11.5px;color:var(--text-muted,#6b7280);margin-top:3px}
	.mbkd-grid{display:grid;grid-template-columns:2fr 1fr;gap:12px}
	@media (max-width:700px){.mbkd-grid{grid-template-columns:1fr}}
	.mbkd-card{background:var(--card-bg,#fff);border:1px solid var(--border-color,#e2e8f0);
		border-radius:12px;padding:16px}
	.mbkd-card-title{font-weight:700;font-size:14px;margin-bottom:12px}
	.mbkd-chart{display:flex;align-items:flex-end;gap:6px;height:160px;direction:ltr}
	.mbkd-bar-wrap{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end}
	.mbkd-bar{width:100%;max-width:34px;background:linear-gradient(180deg,#0E7C86,#0A5A62);
		border-radius:5px 5px 0 0;min-height:3px;transition:height .3s}
	.mbkd-bar-l{font-size:9.5px;color:var(--text-muted,#6b7280);font-family:monospace}
	.mbkd-links{display:flex;flex-direction:column;gap:9px}
	.mbkd-links a{display:block;padding:10px 13px;border-radius:9px;background:var(--control-bg,#f1f5f6);
		color:var(--text-color,#16211f);font-size:13.5px;text-decoration:none;border:1px solid var(--border-color,#e2e8f0)}
	.mbkd-links a:hover{border-color:#0E7C86;color:#0E7C86}
	.mbkd-tablewrap{overflow-x:auto}
	.mbkd-table{width:100%;border-collapse:collapse;font-size:12.5px}
	.mbkd-table th{text-align:right;padding:7px 9px;border-bottom:1.5px solid var(--border-color,#e2e8f0);
		color:var(--text-muted,#6b7280);font-weight:600;white-space:nowrap}
	.mbkd-table td{padding:7px 9px;border-bottom:1px solid var(--border-color,#eef2f3);white-space:nowrap}
	.mbkd-num{font-variant-numeric:tabular-nums;font-family:monospace;direction:ltr;text-align:left}
	.mbkd-empty{text-align:center;color:var(--text-muted,#6b7280);padding:20px}
	`;
	const style = document.createElement('style');
	style.id = 'mbkd-styles';
	style.textContent = css;
	document.head.appendChild(style);
};
