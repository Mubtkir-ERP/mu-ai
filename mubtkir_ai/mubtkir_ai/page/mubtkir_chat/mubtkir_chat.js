/* Full-page shell for the Mubtkir assistant.
 * All chat behavior lives in the shared mubtkir.ChatCore (loaded Desk-wide
 * via app_include_js); this page only provides the frame and toolbar.
 */

frappe.pages['mubtkir-chat'].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __('المساعد الذكي'),
		single_column: true,
	});

	const $shell = $('<div class="mbk-page-shell">').appendTo(page.body);
	const core = new mubtkir.ChatCore({ $container: $('<div>').appendTo($shell) });

	page.set_primary_action(__('محادثة جديدة'), () => core.new_chat(), 'add');
	page.add_inner_button(__('محادثاتي'), () => core.open_history());

	core.focus();
};
