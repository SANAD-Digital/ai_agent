import frappe


def execute():
	"""Stamp the chat-session identity (root_run) on all existing AI Runs."""
	if not frappe.db.has_column("AI Run", "root_run"):
		return

	# Roots: runs with no parent are their own session.
	frappe.db.sql(
		"""
		update `tabAI Run`
		set root_run = name
		where ifnull(parent_run, '') = '' and ifnull(root_run, '') = ''
		"""
	)

	# Children inherit from their parent, one generation per pass.
	for _ in range(50):
		frappe.db.sql(
			"""
			update `tabAI Run` child
			join `tabAI Run` parent on child.parent_run = parent.name
			set child.root_run = parent.root_run
			where ifnull(child.root_run, '') = ''
			  and ifnull(parent.root_run, '') != ''
			"""
		)
		remaining = frappe.db.sql(
			"""
			select 1 from `tabAI Run` child
			join `tabAI Run` parent on child.parent_run = parent.name
			where ifnull(child.root_run, '') = ''
			  and ifnull(parent.root_run, '') != ''
			limit 1
			"""
		)
		if not remaining:
			break

	# Orphans (parent deleted): treat each as its own session root.
	frappe.db.sql(
		"""
		update `tabAI Run`
		set root_run = name
		where ifnull(root_run, '') = ''
		"""
	)
