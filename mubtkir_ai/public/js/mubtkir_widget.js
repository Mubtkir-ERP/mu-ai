/* Mubtkir AI — floating assistant widget.
 *
 * A FAB on every Desk page (gated by the "enable_floating_widget" setting),
 * placed on the side OPPOSITE the sidebar (RTL -> bottom-left, LTR ->
 * bottom-right). It toggles a floating panel hosting the shared
 * mubtkir.ChatCore, so the user never leaves the current page.
 *
 * Panel modes:
 *  - compact (default): draggable via its header; position persisted.
 *  - maximized: full viewport height, docked to the FAB side.
 * Hidden on the full chat page to avoid a duplicate assistant.
 */

frappe.provide('mubtkir');

mubtkir.FloatingWidget = class FloatingWidget {
	static CHAT_PAGE_ROUTE = 'mubtkir-chat';
	static POSITION_KEY = 'mbk_panel_pos';
	static VIEWPORT_MARGIN = 10;

	constructor() {
		this.core = null;
		this.$fab = null;
		this.$panel = null;
		this.maximized = false;
		// Sidebar sits on the reading-start side, so the FAB goes to the end
		// side. Frappe's own RTL check is authoritative; the dir attribute is
		// only a fallback (it is not always set on the html element).
		const is_rtl =
			(frappe.utils && frappe.utils.is_rtl && frappe.utils.is_rtl()) ||
			document.documentElement.getAttribute('dir') === 'rtl';
		this.side = is_rtl ? 'left' : 'right';
	}

	async init() {
		const config = await frappe
			.call({ method: 'mubtkir_ai.api.chat.get_widget_config' })
			.then((r) => r.message)
			.catch(() => null);
		if (!config || !config.enabled) return;

		this.render_fab();
		frappe.router.on('change', () => this.update_visibility());
	}

	/* ------------------------------------------------------------------ FAB */

	render_fab() {
		this.$fab = $(`<button class="mbk-fab mbk-side-${this.side}"
				aria-label="المساعد الذكي" title="المساعد الذكي">${mubtkir.icon('chat', 26)}</button>`)
			.appendTo(document.body)
			.on('click', () => this.toggle());
		// Positional styles go inline so a stale cached stylesheet can
		// never misplace the widget.
		this.$fab.css({ [this.side]: '22px', [this.other_side()]: 'auto' });
		this.update_visibility();
	}

	other_side() {
		return this.side === 'left' ? 'right' : 'left';
	}

	update_visibility() {
		// The router may not have resolved a route yet at first paint.
		const route = frappe.get_route() || [];
		const on_chat_page = route[0] === FloatingWidget.CHAT_PAGE_ROUTE;
		this.$fab.toggle(!on_chat_page);
		if (on_chat_page) this.close();
	}

	set_fab_icon(open) {
		this.$fab.html(mubtkir.icon(open ? 'close' : 'chat', open ? 22 : 26));
	}

	/* ---------------------------------------------------------------- panel */

	toggle() {
		this.$panel ? this.close() : this.open();
	}

	open() {
		this.$panel = $(`
			<div class="mbk-panel mbk-side-${this.side}" role="dialog" aria-label="المساعد الذكي">
				<div class="mbk-p-head">
					<div class="mbk-p-avatar">${mubtkir.icon('chat', 17)}</div>
					<div class="mbk-p-who">
						<div class="mbk-p-t">المساعد الذكي</div>
						<div class="mbk-p-s"><span class="mbk-p-live"></span> متصل · يجيب بلغتك</div>
					</div>
					<button class="mbk-p-btn" data-action="history" title="محادثاتي">${mubtkir.icon('history')}</button>
					<button class="mbk-p-btn" data-action="new" title="محادثة جديدة">${mubtkir.icon('plus')}</button>
					<button class="mbk-p-btn" data-action="max" title="توسيع">${mubtkir.icon('expand')}</button>
					<button class="mbk-p-btn" data-action="close" title="إغلاق">${mubtkir.icon('close')}</button>
				</div>
				<div class="mbk-p-body-holder"></div>
			</div>`).appendTo(document.body);

		// Default anchor beside the FAB, inline for the same cache-immunity.
		this.$panel.css({ [this.side]: '22px', [this.other_side()]: 'auto' });

		this.$panel.on('click', '.mbk-p-btn', (e) => this.handle_action($(e.currentTarget).data('action')));
		this.bind_drag();
		this.apply_saved_position();

		// One core per widget lifetime: reopening keeps the conversation.
		const $holder = this.$panel.find('.mbk-p-body-holder');
		if (this.core) this.core.$container.appendTo($holder);
		else this.core = new mubtkir.ChatCore({ $container: $('<div>').appendTo($holder) });

		if (this.maximized) this.apply_maximized(true);
		this.set_fab_icon(true);
		this.core.focus();
	}

	close() {
		if (!this.$panel) return;
		// Detach (not destroy) the core so the conversation survives reopen.
		this.core.$container.detach();
		this.$panel.remove();
		this.$panel = null;
		this.set_fab_icon(false);
	}

	handle_action(action) {
		if (action === 'close') this.close();
		else if (action === 'new') this.core.new_chat();
		else if (action === 'history') this.core.open_history();
		else if (action === 'max') this.toggle_maximized();
	}

	/* ------------------------------------------------------- maximize mode */

	toggle_maximized() {
		this.apply_maximized(!this.maximized);
	}

	apply_maximized(on) {
		this.maximized = on;
		this.$panel.toggleClass('mbk-panel-max', on);

		// Geometry is applied inline (not via the CSS class) so maximize
		// keeps working even when a stale cached stylesheet is in play.
		if (on) {
			this.$panel.css({
				top: '10px',
				bottom: '10px',
				height: 'auto',
				maxHeight: 'none',
				width: '460px',
				maxWidth: 'calc(100vw - 20px)',
				[this.side]: '10px',
				[this.other_side()]: 'auto',
			});
		} else {
			this.$panel.css({
				top: '', bottom: '', height: '', maxHeight: '', maxWidth: '', width: '',
				[this.side]: '22px', [this.other_side()]: 'auto',
			});
			this.apply_saved_position();
		}

		this.$panel
			.find('[data-action="max"]')
			.attr('title', on ? 'تصغير' : 'توسيع')
			.html(mubtkir.icon(on ? 'shrink' : 'expand'));
	}

	/* ------------------------------------------------- drag (compact mode) */

	bind_drag() {
		const $head = this.$panel.find('.mbk-p-head');

		$head.on('pointerdown', (e) => {
			if (this.maximized || $(e.target).closest('.mbk-p-btn').length) return;

			const rect = this.$panel[0].getBoundingClientRect();
			const offset = { x: e.clientX - rect.left, y: e.clientY - rect.top };

			const on_move = (ev) => this.move_to(ev.clientX - offset.x, ev.clientY - offset.y);
			const on_up = () => {
				$(document).off('pointermove', on_move).off('pointerup', on_up);
				this.save_position();
			};
			$(document).on('pointermove', on_move).on('pointerup', on_up);
			e.preventDefault();
		});
	}

	move_to(left, top) {
		const m = FloatingWidget.VIEWPORT_MARGIN;
		const rect = this.$panel[0].getBoundingClientRect();
		left = Math.min(Math.max(left, m), window.innerWidth - rect.width - m);
		top = Math.min(Math.max(top, m), window.innerHeight - rect.height - m);
		this.$panel.css({ left: `${left}px`, top: `${top}px`, right: 'auto', bottom: 'auto' });
	}

	save_position() {
		const rect = this.$panel[0].getBoundingClientRect();
		localStorage.setItem(FloatingWidget.POSITION_KEY, JSON.stringify({ left: rect.left, top: rect.top }));
	}

	apply_saved_position() {
		try {
			const pos = JSON.parse(localStorage.getItem(FloatingWidget.POSITION_KEY) || 'null');
			if (pos && pos.left < window.innerWidth - 80 && pos.top < window.innerHeight - 80) {
				this.move_to(pos.left, pos.top);
			}
		} catch (e) {
			localStorage.removeItem(FloatingWidget.POSITION_KEY);
		}
	}
};

/* Boot: run after the Desk app is ready (or immediately if it already is). */
(() => {
	let booted = false;
	const boot = () => {
		if (booted || !frappe.session || frappe.session.user === 'Guest') return;
		booted = true;
		new mubtkir.FloatingWidget().init();
	};
	if (window.frappe && frappe.boot) boot();
	$(document).on('app_ready', boot);
})();
