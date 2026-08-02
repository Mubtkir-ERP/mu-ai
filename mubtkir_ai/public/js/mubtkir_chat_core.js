/* Mubtkir AI — shared chat core.
 *
 * One implementation of the chat experience (messages, streaming reveal,
 * report cards, ticket cards, session history, new-chat) reused by both
 * the floating widget and the full Desk page. Shells only provide chrome
 * (header / FAB / page frame) and hand this class a container element.
 */

frappe.provide('mubtkir');

/* Minimal professional icon set (feather-style strokes, currentColor). */
mubtkir.icon = (name, size = 16) =>
	`<svg class="mbk-ic" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"
		stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
		aria-hidden="true">${mubtkir.icon.paths[name] || ''}</svg>`;

mubtkir.icon.paths = {
	chat: '<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
	history: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
	plus: '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
	close: '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
	expand: '<polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/>',
	shrink: '<polyline points="4 14 10 14 10 20"/><polyline points="20 10 14 10 14 4"/><line x1="14" y1="10" x2="21" y2="3"/><line x1="3" y1="21" x2="10" y2="14"/>',
};

mubtkir.ChatCore = class ChatCore {
	static MAX_RENDERED_ROWS = 100;
	static HISTORY_TURNS = 8;
	static REVEAL_DELAY_MS = 28;

	constructor({ $container }) {
		this.$container = $container;
		this.busy = false;
		this.new_chat({ silent: true });
		this.render();
	}

	/* ------------------------------------------------------------ lifecycle */

	render() {
		this.$container.addClass('mbk-chat').html(`
			<div class="mbk-body">
				<div class="mbk-trust">🔒 يرى فقط ما لديك صلاحية الوصول إليه</div>
				<div class="mbk-day">جلسة جديدة</div>
			</div>
			<div class="mbk-composer">
				<div class="mbk-cbox">
					<input type="text" placeholder="اكتب سؤالك… (بالعربي أو الإنجليزي)"
						aria-label="اكتب رسالة" autocomplete="off">
					<button class="mbk-send" aria-label="إرسال">↑</button>
				</div>
				<div class="mbk-hint">يجيب من وثائق ERPNext وبياناتك — ولا يتجاوز صلاحياتك أبدًا</div>
			</div>
		`);

		this.$body = this.$container.find('.mbk-body');
		this.$input = this.$container.find('.mbk-cbox input');
		this.$send = this.$container.find('.mbk-send');

		this.$send.on('click', () => this.send());
		this.$input.on('keydown', (e) => {
			if (e.key === 'Enter' && !e.shiftKey) {
				e.preventDefault();
				this.send();
			}
		});

		this.add_ai_message('مرحبًا 👋 أنا مساعد مُبتكِر. اسألني عن استخدام النظام أو بياناتك أو اطلب تقريرًا.');
	}

	focus() {
		setTimeout(() => this.$input && this.$input.trigger('focus'), 150);
	}

	/* ------------------------------------------------- session / new / load */

	new_chat({ silent = false } = {}) {
		this.session_id = 'mbk-' + frappe.utils.get_random(10);
		if (silent) return;
		this.$body.find('.mbk-msg, .mbk-report, .mbk-sys').remove();
		this.$body.find('.mbk-day').text('جلسة جديدة');
		this.add_ai_message('بدأنا محادثة جديدة ✨ — تفضّل بسؤالك.');
		this.focus();
	}

	load_session(session_id, title) {
		frappe.call({
			method: 'mubtkir_ai.api.chat.get_session_messages',
			args: { session_id },
			callback: (r) => {
				this.session_id = session_id;
				this.$body.find('.mbk-msg, .mbk-report, .mbk-sys').remove();
				this.$body.find('.mbk-day').text(title || 'محادثة سابقة');
				(r.message || []).forEach((m) => {
					if (m.role === 'user') this.add_user_message(m.content);
					else this.add_ai_message(m.content);
				});
				this.close_history();
				this.scroll();
				this.focus();
			},
		});
	}

	/* --------------------------------------------------------- history view */

	open_history() {
		this.close_history();
		const $overlay = $(`
			<div class="mbk-history">
				<div class="mbk-h-head">${mubtkir.icon('history', 16)} محادثاتي
					<button class="mbk-h-close" aria-label="إغلاق">${mubtkir.icon('close', 15)}</button></div>
				<div class="mbk-h-list"><div class="mbk-h-empty">جارِ التحميل…</div></div>
			</div>`);
		$overlay.find('.mbk-h-close').on('click', () => this.close_history());
		this.$container.append($overlay);

		frappe.call({
			method: 'mubtkir_ai.api.chat.get_sessions',
			callback: (r) => {
				const $list = $overlay.find('.mbk-h-list').empty();
				const sessions = r.message || [];
				if (!sessions.length) {
					$list.html('<div class="mbk-h-empty">لا محادثات محفوظة بعد</div>');
					return;
				}
				sessions.forEach((s) => {
					const $item = $(`
						<div class="mbk-h-item" role="button" tabindex="0">
							<div class="mbk-h-title"></div>
							<div class="mbk-h-meta"></div>
						</div>`);
					$item.find('.mbk-h-title').text(s.title);
					$item.find('.mbk-h-meta').text(`${s.message_count} رسالة · ${s.last_activity}`);
					$item.on('click keydown', (e) => {
						if (e.type === 'keydown' && e.key !== 'Enter') return;
						this.load_session(s.session_id, s.title);
					});
					$list.append($item);
				});
			},
		});
	}

	close_history() {
		this.$container.find('.mbk-history').remove();
	}

	/* ------------------------------------------------------------- messaging */

	send() {
		const text = (this.$input.val() || '').trim();
		if (!text || this.busy) return;

		this.busy = true;
		this.$send.prop('disabled', true);
		const history = this.collect_history();
		this.add_user_message(text);
		this.$input.val('');

		const $typing = this.add_typing();

		frappe.call({
			method: 'mubtkir_ai.api.chat.get_reply',
			args: {
				session_id: this.session_id,
				text,
				history,
				page_context: this.collect_page_context(),
			},
			callback: (r) => {
				$typing.remove();
				const d = (r && r.message) || {};
				this.reveal_ai_message(d.reply || 'لا يوجد رد.', d);
			},
			error: () => {
				$typing.remove();
				this.add_ai_message('تعذّر إرسال الرسالة. حاول مجددًا.');
				this.done();
			},
		});
	}

	collect_history() {
		const turns = [];
		this.$body.find('.mbk-msg').each(function () {
			const $m = $(this);
			const content = $m.find('.mbk-txt').text() || $m.find('.mbk-bubble').text();
			if (content) turns.push({ role: $m.hasClass('mbk-me') ? 'user' : 'assistant', content });
		});
		return JSON.stringify(turns.slice(-ChatCore.HISTORY_TURNS));
	}

	/* What the user is currently looking at — collected fresh per message so
	 * navigating while the widget is open always reports the live page. */
	collect_page_context() {
		const route = frappe.get_route() || [];
		const ctx = { route: frappe.get_route_str() };
		if (route[0] === 'Form' && route[1]) {
			ctx.doctype = route[1];
			ctx.docname = route.slice(2).join('/');
		} else if (route[0] === 'List' && route[1]) {
			ctx.doctype = route[1];
			ctx.view = 'list';
		}
		return JSON.stringify(ctx);
	}

	done() {
		this.busy = false;
		this.$send.prop('disabled', false);
		this.focus();
	}

	/* -------------------------------------------------------------- bubbles */

	add_user_message(text) {
		const $m = $('<div class="mbk-msg mbk-me"><div class="mbk-bubble"></div></div>');
		$m.find('.mbk-bubble').text(text);
		this.$body.append($m);
		this.scroll();
	}

	add_ai_message(text, source) {
		const $m = $('<div class="mbk-msg mbk-ai"><div class="mbk-bubble"><span class="mbk-txt"></span></div></div>');
		$m.find('.mbk-txt').text(text);
		if (source) {
			const $cite = $('<div class="mbk-cite">📄 المصدر: <b></b></div>');
			$cite.find('b').text(source);
			$m.append($cite);
		}
		this.$body.append($m);
		this.scroll();
		return $m;
	}

	add_typing() {
		const $t = $('<div class="mbk-msg mbk-ai"><div class="mbk-bubble mbk-typing"><span></span><span></span><span></span></div></div>');
		this.$body.append($t);
		this.scroll();
		return $t;
	}

	/* Gradual word-by-word reveal, then provenance + attachments. */
	reveal_ai_message(fullText, data = {}) {
		const $m = this.add_ai_message('');
		const $txt = $m.find('.mbk-txt');
		const words = fullText.split(' ');
		let i = 0;

		const finish = () => {
			if (data.source) {
				const $cite = $('<div class="mbk-cite">📄 المصدر: <b></b></div>');
				$cite.find('b').text(data.source);
				$m.append($cite);
			}
			if (data.report) this.add_report_card(data.report);
			if (data.created) this.add_created_card(data.created, data.created_url);
			if (data.ticket) this.add_ticket_card(data.ticket, data.ticket_url);
			this.scroll();
			this.done();
		};

		const step = () => {
			if (i >= words.length) return finish();
			$txt.text(words.slice(0, ++i).join(' '));
			this.scroll();
			setTimeout(step, ChatCore.REVEAL_DELAY_MS);
		};
		step();
	}

	/* ---------------------------------------------------------- attachments */

	add_created_card(created, url) {
		const $card = $(`
			<div class="mbk-sys mbk-sys-ok">${mubtkir.icon('plus', 15)}
				<div>أُنشئ <b><span class="mbk-cd-dt"></span> — <span class="mbk-cd-name"></span></b>
					كمسودة بانتظار مراجعتك. <a class="mbk-cd-link" href="#">فتح المستند</a></div>
			</div>`);
		$card.find('.mbk-cd-dt').text(created.doctype);
		$card.find('.mbk-cd-name').text(created.name);
		$card.find('.mbk-cd-link').attr('href', url || '#');
		this.$body.append($card);
		this.scroll();
	}

	add_ticket_card(ticket, ticket_url) {
		const $card = $(`
			<div class="mbk-sys">🎫
				<div>أُنشئت <b>تذكرة دعم #<span class="mbk-tk"></span></b> وحُفظ سياق المحادثة فيها.
					<a class="mbk-tk-link" href="#">عرض التذكرة</a></div>
			</div>`);
		$card.find('.mbk-tk').text(ticket);
		$card.find('.mbk-tk-link').attr('href', ticket_url || '#');
		this.$body.append($card);
		this.scroll();
	}

	add_report_card(report) {
		const cols = report.columns || [];
		const rows = report.rows || [];
		const shown = rows.slice(0, ChatCore.MAX_RENDERED_ROWS);
		const esc = frappe.utils.escape_html;
		const cell = (v) => esc(v === null || v === undefined ? '' : String(v));

		const header = cols.map((c) => `<th>${esc(c.label)}</th>`).join('');
		const body = shown.map((r) =>
			`<tr>${cols.map((c) => `<td class="${c.numeric ? 'mbk-rt-num' : ''}">${cell(r[c.fieldname])}</td>`).join('')}</tr>`
		).join('');

		const totals = report.totals || {};
		const footer = cols.some((c) => c.numeric)
			? `<tr class="mbk-rt-totals">${cols.map((c, i) =>
				`<td class="${c.numeric ? 'mbk-rt-num' : ''}">${c.numeric ? cell(totals[c.fieldname]) : (i === 0 ? 'الإجمالي' : '')}</td>`
			).join('')}</tr>`
			: '';

		const more = rows.length > shown.length
			? `<div class="mbk-rt-more">معروض ${shown.length} من ${rows.length} صفًا — الكامل في ملف CSV</div>`
			: '';

		const $card = $(`
			<div class="mbk-report">
				<div class="mbk-rt-head">
					<span class="mbk-rt-title">📊 ${esc(report.title || 'تقرير')}</span>
					<span class="mbk-rt-count">${rows.length} صف</span>
					<button class="mbk-rt-csv">⬇ CSV</button>
				</div>
				<div class="mbk-rt-wrap">
					<table class="mbk-rt"><thead><tr>${header}</tr></thead>
					<tbody>${body}${footer}</tbody></table>
				</div>
				${more}
			</div>`);

		$card.find('.mbk-rt-csv').on('click', () => this.download_report_csv(report));
		this.$body.append($card);
		this.scroll();
	}

	/* CSV built client-side; UTF-8 BOM keeps Arabic intact in Excel. */
	download_report_csv(report) {
		const cols = report.columns || [];
		const quote = (v) => `"${String(v === null || v === undefined ? '' : v).replace(/"/g, '""')}"`;
		const lines = [
			cols.map((c) => quote(c.label)).join(','),
			...(report.rows || []).map((r) => cols.map((c) => quote(r[c.fieldname])).join(',')),
		];
		const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = `${(report.title || 'report').replace(/[\\/:*?"<>|]/g, '_')}.csv`;
		a.click();
		URL.revokeObjectURL(a.href);
	}

	scroll() {
		if (this.$body && this.$body[0]) this.$body[0].scrollTop = this.$body[0].scrollHeight;
	}
};
