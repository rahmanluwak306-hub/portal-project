from odoo import api, fields, models, _
from datetime import datetime, date, timedelta, timezone
from odoo.exceptions import ValidationError
from odoo.tools.float_utils import float_compare
from odoo.tools.translate import _
from html import unescape
import time
import re


class ProjectTask(models.Model):

    _inherit = "project.task"
    _order = "id asc"

    def _reindex_subtasks(self):
        for idx, child in enumerate(self.child_ids.sorted("create_date"), start=1):
            child.name = f"{self.name}.{str(idx).zfill(2)}"
            child._reindex_subtasks()

    def get_all_subtasks_inclusive(self):
        """Helper untuk ambil task + semua subtasks (recursive)."""
        self.ensure_one()
        all_tasks = self
        if self.child_ids:
            for child in self.child_ids:
                all_tasks |= child.get_all_subtasks_inclusive()
        return all_tasks

    @api.depends(
        'timesheet_ids.z_timesheet_start_date',
        'timesheet_ids.z_timesheet_end_date',
        'child_ids.z_actual_start_date',
        'child_ids.z_actual_end_date'
    )
    def _compute_actual_dates(self):
        for task in self:
            all_tasks = task.get_all_subtasks_inclusive()
            all_lines = all_tasks.mapped("timesheet_ids")
            start_dates = [d for d in all_lines.mapped("z_timesheet_start_date") if d]
            end_dates = [d for d in all_lines.mapped("z_timesheet_end_date") if d]
            task.z_actual_start_date = min(start_dates) if start_dates else False
            task.z_actual_end_date = max(end_dates) if end_dates else False

    @api.depends(
        'z_project_task_state',
        'z_progress_project_entry',
        'child_ids',
        'child_ids.z_progress_project',
        'child_ids.z_project_task_state',
        'child_ids.z_progress_project_entry',
    )
    def _compute_progress(self):
        for this in self:
            progress = 0
            if this.child_ids:
                child_ids = this.child_ids
                progress_avg = 100 / len(child_ids)
                progress_entry = 0
                for x in child_ids:
                    if x.z_end_task_ok:
                        progress_entry += ((progress_avg / 100 * x.z_progress_project_entry / 100) * 100)
                    else:
                        progress_entry += ((progress_avg / 100 * x.z_progress_project / 100) * 100)
                progress += progress_entry
            else:
                if this.z_project_task_state != 'done':
                    progress += this.z_progress_project_entry
                else:
                    progress += 100
            this.z_progress_project = progress

    @api.depends(
        'project_id.z_mandays_budget',
        'project_id.task_ids',
        'parent_id.z_mandays_budget_entry',
        'parent_id.child_ids',
    )
    def _getMandaysBudget(self):
        for this in self:
            mandays = this.project_id.z_mandays_budget or 0
            clean_mandays = mandays
            if clean_mandays and this.parent_id.child_ids:
                mandays = this.parent_id.z_mandays_budget_entry or 0
                clean_mandays = mandays / len(this.parent_id.child_ids)
            elif clean_mandays and this.project_id.task_ids:
                clean_mandays = mandays / len(this.project_id.task_ids.filtered(lambda x: not x.parent_id))
            this.z_mandays_budget = clean_mandays

    @api.depends(
        'z_bobot_entry',
        'project_id.z_mandays_budget',
        'project_id.task_ids',
        'parent_id.z_bobot_entry',
        'parent_id.z_mandays_budget_entry',
        'parent_id.child_ids',
    )
    def _getMandaysBudgetEntry(self):
        for this in self:
            mandays = 0
            if not this.parent_id:
                mandays += this.project_id.z_mandays_budget * this.z_bobot / 100
            else:
                mandays += this.parent_id.z_mandays_budget_entry * this.z_bobot / 100
            this.z_mandays_budget_entry = mandays

    @api.depends(
        'timesheet_ids.unit_amount',
        'child_ids.timesheet_ids.unit_amount'
    )
    def _getActualMandaysBudget(self):
        for this in self:
            # self task and all subtask
            all_tasks = this.get_all_subtasks_inclusive()
            actual_mandays = all_tasks.mapped('timesheet_ids').filtered(lambda x: x.z_timesheet_start_date and x.z_timesheet_end_date)
            actual_mandays = sum(actual_mandays.mapped('unit_amount')) / 8
            this.z_actual_budget_mandays = actual_mandays

    @api.depends('child_ids')
    def _getSubtaskCount(self):
        for this in self:
            task_done = len(this.child_ids.filtered(lambda x: x.z_project_task_state == 'done'))
            this.z_subtask_done_count = task_done / len(this.child_ids) * 100 if task_done and this.child_ids else 0
            this.z_subtask_count = len(this.child_ids)

    @api.depends('timesheet_ids')
    def _getTimesheetCount(self):
        for this in self:
            timesheet_done = len(this.timesheet_ids.filtered(lambda x: x.z_state == 'approved'))
            this.z_timesheet_done_count = timesheet_done / len(this.timesheet_ids) * 100 if timesheet_done and this.timesheet_ids else 0
            this.z_timesheet_count = len(this.timesheet_ids)

    @api.onchange('z_bobot_entry')
    def onchange_bobot_entry(self):
        for this in self:
            bobot = 0
            if this.parent_id:
                bobot = this.z_bobot_entry / this.parent_id.z_bobot_entry * 100 if this.z_bobot_entry and this.parent_id.z_bobot_entry else 0
            elif this.project_id:
                bobot = this.z_bobot_entry if this.z_bobot_entry else 0
            this.z_bobot = bobot

    def _compute_running_duration(self):
        for this in self:
            this.z_running_duration = "00:00:00"
            if this.z_time_start:
                start = fields.Datetime.from_string(this.z_time_start).replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                delta = now - start
                total_seconds = int(delta.total_seconds())
                total_hours = total_seconds / 3600
                minutes = (total_seconds % 3600) / 60
                seconds = total_seconds % 60
                this.z_running_duration = f"{total_hours:02}:{minutes:02}:{seconds:02}"

    def _getTimeStart(self):
        for this in self:
            time_start = False
            employee_ids = self.env['hr.employee'].sudo().search([('user_id', '=', self.env.user.id)], limit=1)
            if employee_ids:
                timesheet_ids = this.timesheet_ids.filtered(lambda x: x.employee_id.id == employee_ids.id and not x.z_timesheet_end_date)
                if timesheet_ids:
                    time_start = timesheet_ids.z_timesheet_start_date
            this.z_time_start = time_start

    @api.depends('project_id.label_tasks')
    def _compute_name_of_project(self):
        """Get name of project from project.label_tasks"""
        for task in self:
            task.z_name_of_project = task.project_id.label_tasks if task.project_id else ''

    @api.depends('project_id.z_type_in_project')
    def _compute_project_type_visibility(self):
        """Determine if maintenance fields should be visible"""
        for task in self:
            task.z_is_maintenance = (task.project_id.z_type_in_project == 'maintenance') if task.project_id else False

    @api.depends(
        'z_master_task_id',
        'z_master_task_id.z_severity_ids',
    )
    def _getSeverityIds(self):
        for this in self:
            severity_ids = this.z_master_task_id.z_severity_ids
            this.z_severity_ids = [(6, 0, severity_ids.ids)]

    @api.depends(
        'z_master_task_id',
        'z_master_task_id.z_technology_ids',
    )
    def _getTechnologyIds(self):
        for this in self:
            technology_ids = this.z_master_task_id.z_technology_ids
            this.z_technology_ids = [(6, 0, technology_ids.ids)]

    @api.depends(
        'project_id',
        'project_id.z_project_teams2_ids',
        'project_id.z_project_teams2_ids.z_project_teams_employee_id',
        'tag_ids'
    )
    def _getProjectTeams(self):
        for this in self:
            project_teams = this.project_id.z_project_teams2_ids.mapped('z_project_teams_employee_id.id')
            this.z_project_teams_ids = [(6, 0, project_teams)]

    @api.depends('parent_id', 'parent_id.z_master_task_id', 'child_ids')
    def _getParentMasterTask(self):
        for rec in self:
            parent = rec.parent_id
            while parent and parent.parent_id:
                parent = parent.parent_id
            rec.z_parent_master_task_id = parent.z_master_task_id.id if parent and parent.z_master_task_id else False

    @api.depends('child_ids')
    def _getEndTask(self):
        for this in self:
            this.z_end_task_ok = True if not this.child_ids else False

    @api.depends(
        'z_mandays_budget_entry',
        'z_actual_budget_mandays',
    )
    def _getQualityCalculation(self):
        for this in self:
            quality = 0
            if this.z_mandays_budget_entry and this.z_actual_budget_mandays and this.z_project_task_state == 'done':
                quality += (this.z_mandays_budget_entry / this.z_actual_budget_mandays * 100)
            this.z_quality_calculation = quality

    @api.depends('z_master_task_id', 'z_master_task_free_text')
    def _compute_display_master_task(self):
        for rec in self:
            rec.z_display_master_task = False
            if rec.z_master_task_free_text:
                rec.z_display_master_task = rec.z_master_task_free_text
            elif rec.z_master_task_id:
                rec.z_display_master_task = rec.z_master_task_id.z_name

    @api.onchange('z_master_task_self_create_ok')
    def onchange_task_exist(self):
        for this in self:
            this.z_master_task_free_text = False
            this.z_master_task_id = False

    # override
    name = fields.Char("Title", required=False)
    project_id = fields.Many2one('project.project', string='Projects',domain="['|', ('company_id', '=', False), ('company_id', '=?',  company_id)]",
         compute="_compute_project_id", store=True, precompute=True, recursive=True,readonly=False, index=True, tracking=True, change_default=True)
    date_deadline = fields.Datetime(string='Deadline Date', index=True, tracking=True)
    # added
    z_reason_reject_description = fields.Char(string='Description Reject')
    z_master_task_free_text = fields.Char(string='Name of Tasks Entries')
    z_master_task_self_create_ok = fields.Boolean(string='Not Tasks Exist?',default=False)
    z_display_master_task = fields.Char(string="Name of tasks", compute=_compute_display_master_task, store=True)
    z_end_task_ok = fields.Boolean(string='End Task',compute=_getEndTask,store=False)
    z_project_name = fields.Char(string='Name of The Projects',related='project_id.label_tasks',store=True,translate=True)
    z_group_type_project = fields.Selection([
        ('project', 'Project'),
        ('non_project', 'Non Project')
    ], string='Group', related='project_id.z_group_type_project', store=True)
    z_type_in_project = fields.Selection([
        ('delivery', 'Delivery'),
        ('maintenance', 'Maintenance')
    ], string='Type Of Project', related='project_id.z_type_in_project', store=True)
    z_type_non_project = fields.Selection([
        ('others', 'Others'),
        ('ticket', 'Ticket')
    ], string='Type Of Non Project', related='project_id.z_type_non_project', store=True)
    z_value_project = fields.Float(string="Value Project (Budget)")
    z_regional_id = fields.Many2one('area.regional', string='Regional')
    z_technology_id = fields.Many2one('technology.used', string='Technology')
    z_severity_id = fields.Many2one('severity.master', string='Severity')
    z_mandays_budget = fields.Float(string="Budget Mandays", compute=_getMandaysBudget, store=True)
    z_mandays_budget_entry = fields.Float(string='Budget Mandays', compute=_getMandaysBudgetEntry, store=True)
    z_actual_budget_mandays = fields.Float(string="Actual Mandays", compute=_getActualMandaysBudget, store=True)
    z_bobot = fields.Float(string="Bobot Big (%)", store=True, digits=(12, 12))
    z_bobot_entry = fields.Float(string="Bobot (%)", digits=(12, 2))
    z_progress_project = fields.Float(string="Progress", compute="_compute_progress", store=True, digits=(12, 2))
    z_progress_project_entry = fields.Float(string="Progress (Entry)", digits=(12, 2))
    z_quality_calculation = fields.Float(string="Quality (%)", compute=_getQualityCalculation, store=True)
    z_quality_entry = fields.Float(string="Quality Entry (%)")
    z_master_task_id = fields.Many2one('task.master', string='Name of The Tasks')
    z_parent_master_task_id = fields.Many2one('task.master', string='Name of The Tasks',compute=_getParentMasterTask,store=True)
    z_head_assignes_ids = fields.Many2many(comodel_name="hr.employee",relation="task_head_employee_rel",column2="employee_id",string="Head Assignees")
    z_member_assignes_ids = fields.Many2many(comodel_name="hr.employee",relation="task_member_employee_rel",column1="task_id",column2="employee_id",string="Member Assignees")
    z_project_task_state = fields.Selection([
        ('new', 'New'),
        ('in_progress', 'In Progress'),
        ('approved1', 'The First Approved'),
        ('approved2', 'The Second Approved'),
        ('done', 'Done'),
        ('reject', 'Rejected'),
        ('cancel', 'Cancelled'),
    ], string='Status', default="new")
    z_project_type = fields.Selection(related="project_id.z_type_in_project",string="Type Of Project",store=True,readonly=True)
    z_planned_start_date = fields.Date(string='Planned Start Date')
    z_planned_end_date = fields.Date(string='Planned End Date')
    z_actual_start_date = fields.Date(string='Actual Start Date', compute="_compute_actual_dates" ,store=True)
    z_actual_end_date = fields.Date(string='Actual End Date', compute="_compute_actual_dates", store=True)
    z_invoice_plan_ids = fields.One2many('project.task.invoice.plan','z_invoce_plan_id',string='Invoice Plan')
    z_missing_from = fields.Char(string="Missing From",readonly=True,help="Menunjukkan task ini dibuat karena ada gap dari nomor tertentu.")
    z_description = fields.Char('Description')
    z_subtask_count = fields.Integer(string='Count', compute=_getSubtaskCount, store=False)
    z_subtask_done_count = fields.Integer(string='Done Percent', compute=_getSubtaskCount, store=False)
    z_timesheet_count = fields.Integer(string='Count', compute=_getTimesheetCount, store=False)
    z_timesheet_done_count = fields.Integer(string='Done Percent', compute=_getTimesheetCount, store=False)
    z_time_start = fields.Datetime(string="Time Start",compute=_getTimeStart,store=False)
    z_running_duration = fields.Char(string="Running Duration",compute=_compute_running_duration,store=False)
    z_name_of_project = fields.Char(string='Name of Project', compute='_compute_name_of_project', store=True,readonly=True)
    z_is_maintenance = fields.Boolean(string='Is Maintenance Project', compute='_compute_project_type_visibility',store=True)
    z_timer_attachment_ids = fields.Many2many('ir.attachment', 'task_timer_attachment_rel', 'task_id', 'attachment_id',string='Timer Attachments')
    z_severity_ids = fields.Many2many('severity.master','project_task_severity_ids_rel',string='Severity',compute=_getSeverityIds,store=False)
    z_technology_ids = fields.Many2many('technology.used','project_task_technology_ids_rel',string='Technology',compute=_getTechnologyIds,store=False)
    z_project_teams_ids = fields.Many2many('hr.employee', relation='project_task_project_teams_ids_rel',string='Project Teams',compute=_getProjectTeams,store=False)
    # not used
    z_time_end = fields.Datetime(string='Time End')
    z_bobot_big = fields.Float(string='Bobot Big (%)')

    @api.model
    def create(self, vals):
        task = super().create(vals)
        if task.parent_id:
            task.generate_sequence_name()
            task.parent_id.message_post(
                body=_("Subtask %s dibuat di bawah %s.") % (
                    task.display_name, task.parent_id.display_name
                )
            )
        else:
            task.generate_project_sequence_name()
        return task

    def write(self, values):
        res = super(ProjectTask, self).write(values)
        for this in self:
            this.action_bobot_sync()
        return res

    def unlink(self):
        parents = self.mapped("parent_id")
        deleted_names = self.mapped("display_name")
        res = super().unlink()
        for parent in parents:
            parent.message_post(
                body=_("Subtask %s dihapus dari parent %s.") % (
                    ", ".join(deleted_names), parent.display_name
                )
            )
        return res

    def action_bobot_sync(self):
        if self.parent_id:
            task_ids = self.parent_id.child_ids
            bobot_entry = round(sum(task_ids.mapped('z_bobot_entry')), 1)
        else:
            task_ids = self.project_id.task_ids.filtered(lambda x: not x.parent_id)
            bobot_entry = round(sum(task_ids.mapped('z_bobot_entry')), 1)
        if bobot_entry > 100:
            raise ValidationError('Percentage bobot > 100%')
        subtask_ids = self.env['project.task'].sudo().search([('project_id','=',self.project_id.id),('parent_id','child_of',self.id),('id','!=',self.id)], order='id asc')
        for x in subtask_ids:
            bobot_entry = x.parent_id.z_bobot_entry * x.z_bobot / 100
            x.z_bobot_entry = bobot_entry

    def action_bobot_calc_rate(self):
        if self.child_ids:
            child_ids = self.child_ids.filtered(lambda x: x.parent_id.id == self.id)
            bobot = 100 / len(child_ids)
            bobot_entry = self.z_bobot_entry * bobot / 100
            for x in child_ids:
                x.z_bobot = bobot
                x.z_bobot_entry = bobot_entry
            id_subtask = child_ids.ids + [self.id]
            subtask_ids = self.env['project.task'].sudo().search([('project_id','=',self.project_id.id),('parent_id','child_of',self.id),('id','not in',id_subtask)], order='id asc')
            for x in subtask_ids:
                bobot_entry = x.parent_id.z_bobot_entry * x.z_bobot / 100
                x.z_bobot_entry = bobot_entry

    def action_request_timesheet(self):
        employee_ids = self.env['hr.employee'].sudo().search([('user_id','=',self.env.user.id)], order='id desc', limit=1)
        values = {
            'default_partner_id': self.partner_id.id,
            'default_project_id': self.project_id.id,
            'default_employee_id': employee_ids.id if employee_ids else False,
            'default_task_id': self.id,
            'default_company_id': self.env.user.company_id.id,
            'default_z_state': 'waiting_approval',
        }
        return {
            "name": _("Request Timesheet"),
            "type": "ir.actions.act_window",
            "res_model": "account.analytic.line",
            "view_mode": "form",
            "domain": [],
            "context": values,
            'views': [
                [self.env.ref('z_project.z_account_analytic_line_form').id, 'form'],
            ],
        }

    def action_create_subtask(self):
        self.env['project.task'].create({
            'parent_id': self.id,
            'partner_id': self.partner_id.id,
            'project_id': self.project_id.id,
            'z_head_assignes_ids': [(6, 0, self.z_head_assignes_ids.ids)],
            'z_member_assignes_ids': [(6, 0, self.z_member_assignes_ids.ids)],
            'tag_ids': [(6, 0, self.tag_ids.ids)],
            'z_planned_start_date': datetime.now(),
            'z_planned_end_date': datetime.now(),
            'date_deadline': datetime.now(),
        })
        self.action_view_tasks()

    def action_view_tasks(self):
        return {
            "name": _("Tasks"),
            "type": "ir.actions.act_window",
            "res_model": "project.task",
            "view_mode": "list,form",
            "domain":[('id','in',self.child_ids.ids)],
            "context": {'active_model': 'project.project', 'default_project_id': self.project_id.id, 'default_parent_id': self.id},
            'views': [
                [self.env.ref('z_project.z_project_task_list').id, 'list'],
                [self.env.ref('z_project.z_project_task_form_inherit_project').id, 'form'],
            ],
        }

    def action_view_timesheets(self):
        return {
            "name": _("Timesheet"),
            "type": "ir.actions.act_window",
            "res_model": "account.analytic.line",
            "view_mode": "list,form",
            "domain": [('id', 'in', self.timesheet_ids.ids)],
            "context": {},
            'views': [
                [self.env.ref('z_project.z_account_analytic_line_list').id, 'list'],
                [self.env.ref('z_project.z_account_analytic_line_form').id, 'form'],
            ],
        }

    def action_confirm(self):
        if self.z_project_task_state == 'new':
            self.z_project_task_state = 'in_progress'
        elif self.z_project_task_state == 'in_progress':
            if self.timesheet_ids and self.timesheet_ids.filtered(lambda x: x.z_state != 'approved'):
                raise ValidationError('Masih terdapat Timesheets yang belum selesai ({} timesheet).'.format(
                    len(self.timesheet_ids.filtered(lambda x: x.z_state != 'approved'))))
            if self.child_ids and self.child_ids.filtered(lambda x: x.z_project_task_state != 'done'):
                raise ValidationError('Masih terdapat Sub-tasks yang belum selesai ({} tasks).'.format(
                    len(self.child_ids.filtered(lambda x: x.z_project_task_state != 'done'))))
            self.z_project_task_state = 'approved1'
        elif self.z_project_task_state == 'approved1':
            self.z_project_task_state = 'approved2'
        elif self.z_project_task_state == 'approved2':
            self.z_project_task_state = 'done'
            self.z_progress_project_entry = 100

    def action_reject(self):
        self.z_project_task_state = 'in_progress'

    def action_set_to_draft(self):
        self.z_project_task_state = 'new'

    def action_start_timesheet(self):
        employee = self.env['hr.employee'].sudo().search([('user_id', '=', self.env.user.id)], limit=1)
        if not employee:
            raise ValidationError('Kamu tidak masuk dalam data karyawan. Silahkan hubungi administrator.')
        if self.z_project_task_state == 'new':
            self.z_project_task_state = 'in_progress'
        timesheet = self.env['account.analytic.line'].sudo().search([
            ('id', 'in', self.timesheet_ids.ids),
            ('task_id', '=', self.id),
            ('employee_id', '=', employee.id),
            ('z_timesheet_end_date', '=', False)
        ], limit=1)
        if timesheet:
            raise ValidationError('Terdapat timesheet yang belum selesai.')
        self.action_timer_start()
        self.timesheet_ids = [(0, 0, {
            'z_timesheet_start_date': datetime.now(),
            'employee_id': employee.id,
            'name': self.z_description,
            'z_state': 'draft',
        })]

    def action_end_timesheet(self):
        employee_ids = self.env['hr.employee'].sudo().search([('user_id', '=', self.env.user.id)], limit=1)
        if not employee_ids:
            raise ValidationError('Kamu tidak masuk dalam data karyawan. Silahkan hubungi administrator.')
        timesheet_ids = self.env['account.analytic.line'].sudo().search([('id','in',self.timesheet_ids.ids),('employee_id','=',employee_ids.id)], order='id desc', limit=1)
        if not timesheet_ids:
            raise ValidationError('Data timesheet tidak tersedia.')
        elif timesheet_ids.z_timesheet_end_date:
            raise ValidationError('Semua timesheet sudah selesai.')
        else:
            if not self.env.context.get('confirmTimeEnd', False):
                return {
                    "name": _("Time End"),
                    "type": "ir.actions.act_window",
                    "res_model": "project.task",
                    "view_mode": "form",
                    "target": "new",
                    "res_id": self.id,
                    "domain": [],
                    "context": {},
                    'views': [
                        [self.env.ref('z_project.z_project_task_form_timer').id, 'form'],
                    ],
                }
            timesheet_ids.write({
                'z_timesheet_end_date': datetime.now(),
                'name': self.z_description,
                'z_state': 'approved',
            })

    # not used
    def action_finish_task(self):
        if self.child_ids.filtered(lambda x: x.z_project_task_state != 'done'):
            raise ValidationError('Masih terdapat Sub-Task yang belum selesai.')
        self.z_project_task_state = 'done'

    def generate_sequence_name(self):
        """Generate nama otomatis untuk subtask + deteksi gap"""
        if self.parent_id:
            siblings = self.parent_id.child_ids.filtered(lambda t: t.id != self.id)
            existing_numbers = []
            for sib in siblings:
                if sib.name and sib.name.startswith(self.parent_id.name + "."):
                    try:
                        num = int(sib.name.split(".")[-1])
                        existing_numbers.append(num)
                    except:
                        pass
            if existing_numbers:
                next_number = max(existing_numbers) + 1
                missing = set(range(1, next_number)) - set(existing_numbers)
                self.name = f"{self.parent_id.name}.{str(next_number).zfill(2)}"
                return min(missing) if missing else False
            else:
                self.name = f"{self.parent_id.name}.01"
                return False

    def generate_project_sequence_name(self):
        if self.project_id:
            siblings = self.project_id.task_ids.filtered(lambda t: not t.parent_id and t.id != self.id)
            existing_numbers = []
            for sib in siblings:
                if sib.name and sib.name.startswith("T-"):
                    try:
                        num = int(sib.name.split("-")[-1])
                        existing_numbers.append(num)
                    except:
                        pass
            if existing_numbers:
                next_number = max(existing_numbers) + 1
                missing = set(range(1, next_number)) - set(existing_numbers)
                self.name = f"T-{str(next_number).zfill(2)}"
                return min(missing) if missing else False
            else:
                self.name = "T-01"
                return False

    def action_view_subtask(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("project.project_task_action_sub_task")
        action["domain"] = [("parent_id", "=", self.id)]
        action["context"] = {
            "default_parent_id": self.id,
            "default_project_id": self.project_id.id,
            "group_by": "z_project_task_state",
        }
        return action

    def action_view_subtask(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id("project.project_task_action_sub_task")
        action["domain"] = [("parent_id", "=", self.id)]
        action["context"] = {
            "default_parent_id": self.id,
            "default_project_id": self.project_id.id,
            "group_by": "z_project_task_state",
        }
        return action

    # not used
    def action_recalculate_bobot(self):
        print('a')

    # not used
    def action_recalculate_all(self):
        print('a')

    # not used
    def action_check_bobot(self):
        print('a')


class AccountAnalyticLine(models.Model):

    _name = 'account.analytic.line'
    _inherit = ['account.analytic.line', 'mail.thread', 'mail.activity.mixin']

    @api.depends('z_timesheet_start_date', 'z_timesheet_end_date')
    def _compute_unit_amount(self):
        for line in self:
            line.unit_amount = 0.0
            if line.z_timesheet_start_date and line.z_timesheet_end_date:
                timesheet_start = line.z_timesheet_start_date + timedelta(hours=7)
                timesheet_start = timesheet_start.replace(second=0, microsecond=0)
                timesheet_end = line.z_timesheet_end_date + timedelta(hours=7)
                timesheet_end = timesheet_end.replace(second=0, microsecond=0)
                day_week = str(timesheet_start.weekday())
                working_schedule = self.env['resource.calendar'].sudo().search([('tz', '=', 'Asia/Jakarta')], order='id asc', limit=1)
                if working_schedule and working_schedule.attendance_ids:
                    morning = working_schedule.attendance_ids.filtered(lambda x: x.dayofweek == day_week and x.day_period == 'morning')
                    afternoon = working_schedule.attendance_ids.filtered(lambda x: x.dayofweek == day_week and x.day_period == 'afternoon')
                    work_from = False
                    work_to =False
                    if morning and afternoon:
                        work_from = fields.Datetime.from_string(timesheet_start).replace(hour=int(morning.hour_to), minute=0, second=0, microsecond=0)
                        work_to = fields.Datetime.from_string(timesheet_start).replace(hour=int(afternoon.hour_from), minute=0, second=0, microsecond=0)
                    if morning and afternoon and timesheet_end < work_to:
                        result = (timesheet_end - timesheet_start).total_seconds() / 3600
                        line.unit_amount = result
                    elif morning and afternoon and timesheet_end > work_to:
                        work_morning = (work_from - timesheet_start).total_seconds() / 3600
                        work_afternoon = (timesheet_end - work_to).total_seconds() / 3600
                        if timesheet_start > work_to:
                            work_afternoon = (timesheet_end - timesheet_start).total_seconds() / 3600
                        result = 0
                        if work_morning and work_morning > 0:
                            result += work_morning
                        if work_afternoon and work_afternoon > 0:
                            result += work_afternoon
                        line.unit_amount = result

    # override
    unit_amount = fields.Float(string='Time Spent (Hours)', compute="_compute_unit_amount", store=True, readonly=False,digits=(12, 2))
    # added
    z_timer_state = fields.Selection([
        ('running', 'Running'),
        ('paused', 'Paused'),
        ('stopped', 'Stopped'),
    ], string='Timer State', default='stopped', tracking=True)
    z_is_paused = fields.Boolean(string='Paused', default=False)
    z_pause_started_at = fields.Datetime(string='Pause Started At')
    z_pause_accum_seconds = fields.Integer(string='Pause Seconds', default=0)
    z_timesheet_start_date = fields.Datetime(string='Start Date')
    z_timesheet_end_date = fields.Datetime(string='End Date')
    z_timesheet_spent = fields.Float(string='Time Spent')
    z_line_ids = fields.One2many('account.analytic.line.request', 'z_timesheet_id', string='Lines')
    z_pause_history = fields.Text(string='Pause History')
    z_state = fields.Selection([
        ('draft', 'Draft'),
        ('waiting_approval', 'Waiting Approval'),
        ('approved', 'Done'),
        ('reject', 'Rejected'),
    ], string='Status', default='draft')

    def action_approve(self):
        self.z_state = 'approved'

    def action_reject(self):
        self.z_state = 'reject'

    def action_view(self):
        return {
            "name": _("Request Timesheet"),
            "type": "ir.actions.act_window",
            "res_model": "account.analytic.line",
            "view_mode": "form",
            "res_id": self.id,
            "domain": [],
            "context": {},
            'views': [
                [self.env.ref('z_project.z_account_analytic_line_form').id, 'form'],
            ],
        }

    # TIMER METHODS
    def action_pause_timer(self):
        now = fields.Datetime.now()
        for line in self:
            if line.z_timesheet_end_date or line.z_is_paused:
                continue
            line.write({
                'z_is_paused': True,
                'z_pause_started_at': now,
                'z_timer_state': 'paused',
            })

    def action_resume_timer(self):
        now = fields.Datetime.now()
        for line in self:
            if line.z_timesheet_end_date or not line.z_is_paused:
                continue
            accum = line.z_pause_accum_seconds or 0
            if line.z_pause_started_at:
                accum += int((now - line.z_pause_started_at).total_seconds())
            line.write({
                'z_is_paused': False,
                'z_pause_started_at': False,
                'z_pause_accum_seconds': accum,
                'z_timer_state': 'running',
            })


class AccountAnalyticLineRequest(models.Model):
    
    _name = "account.analytic.line.request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "Request Timesheet"
    _rec_name = "z_name"
    _order = "id asc"

    @api.depends(
        'z_current_start_date',
        'z_current_end_date',
    )
    def _getCurrentTimeSpent(self):
        for line in self:
            line.z_current_time_spent = 0.0
            if line.z_current_start_date and line.z_current_end_date:
                timesheet_start = line.z_current_start_date + timedelta(hours=7)
                timesheet_start = timesheet_start.replace(second=0, microsecond=0)
                timesheet_end = line.z_current_end_date + timedelta(hours=7)
                timesheet_end = timesheet_end.replace(second=0, microsecond=0)
                day_week = str(timesheet_start.weekday())
                working_schedule = self.env['resource.calendar'].sudo().search([('tz', '=', 'Asia/Jakarta')], order='id asc', limit=1)
                if working_schedule and working_schedule.attendance_ids:
                    morning = working_schedule.attendance_ids.filtered(lambda x: x.dayofweek == day_week and x.day_period == 'morning')
                    afternoon = working_schedule.attendance_ids.filtered(lambda x: x.dayofweek == day_week and x.day_period == 'afternoon')
                    work_from = False
                    work_to = False
                    if morning and afternoon:
                        work_from = fields.Datetime.from_string(timesheet_start).replace(hour=int(morning.hour_to), minute=0, second=0, microsecond=0)
                        work_to = fields.Datetime.from_string(timesheet_start).replace(hour=int(afternoon.hour_from), minute=0, second=0, microsecond=0)
                    if morning and afternoon and timesheet_end < work_to:
                        result = (timesheet_end - timesheet_start).total_seconds() / 3600
                        line.z_current_time_spent = result
                    elif morning and afternoon and timesheet_end > work_to:
                        work_morning = (work_from - timesheet_start).total_seconds() / 3600
                        work_afternoon = (timesheet_end - work_to).total_seconds() / 3600
                        if timesheet_start > work_to:
                            work_afternoon = (timesheet_end - timesheet_start).total_seconds() / 3600
                        result = 0
                        if work_morning and work_morning > 0:
                            result += work_morning
                        if work_afternoon and work_afternoon > 0:
                            result += work_afternoon
                        line.z_current_time_spent = result

    z_task_id = fields.Many2one('project.task', string='Task', index=True)
    z_timesheet_id = fields.Many2one('account.analytic.line', string='Timesheet', ondelete='cascade')
    z_employee_id = fields.Many2one('hr.employee', string='Employee')
    z_name = fields.Char(string='Description')
    z_current_start_date = fields.Datetime(string='Actual Start')
    z_current_end_date = fields.Datetime(string='Actual End')
    z_current_time_spent = fields.Float(string='Actual Spent', compute=_getCurrentTimeSpent, store=True)
    z_state = fields.Selection([
        ('waiting_approval', 'Waiting Approval'),
        ('approved', 'Done'),
        ('rejected', 'Rejected'),
    ], string='Status', default='waiting_approval')
    z_request_type = fields.Selection([
        ('new', 'New Entry'),
        ('correction', 'Correction'),
    ], required=True, default='correction', tracking=True)
    # not used
    z_ori_start_date = fields.Datetime(string='Original Start Date')
    z_ori_end_date = fields.Datetime(string='Original End Date')
    z_ori_time_spent = fields.Float(string='Original Time Spent')

    def action_approve(self):
        if not (self.z_current_start_date and self.z_current_end_date):
            raise ValidationError("Start / End required.")
        self.z_state = 'approved'
        timesheet = self.z_timesheet_id
        if timesheet:
            timesheet.sudo().write({
                'z_timesheet_start_date': self.z_current_start_date,
                'z_timesheet_end_date': self.z_current_end_date,
                'name': self.z_name,
                'date': self.z_current_start_date.date(),
                'z_state': 'approved',
            })
            timesheet.sudo()._compute_unit_amount()
            timesheet.z_state = 'approved'
        else:
            timesheet = self.env['account.analytic.line'].sudo().create({
                'task_id': self.z_task_id.id,
                'z_timesheet_start_date': self.z_current_start_date,
                'z_timesheet_end_date': self.z_current_end_date,
                'name': self.z_name,
                'date': self.z_current_start_date.date(),
                'z_state': 'approved',
            })
            timesheet.write({
                'employee_id': self.z_employee_id.id,
                'project_id': timesheet.task_id.project_id.id,
            })
            timesheet.sudo()._compute_unit_amount()
            timesheet.z_state = 'approved'

    def action_reject(self):
        self.z_state = 'rejected'


class ProjectTaskInvoicePlan(models.Model):

    _name = "project.task.invoice.plan"
    _description = "Invoice Plan"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "z_name"
    _order = "id desc"

    z_invoce_plan_id = fields.Many2one('project.task', string='Project', ondelete='cascade')
    z_name = fields.Char(string='Invoice Description')
    z_number_of_invoice = fields.Char(string='No. Invoice')
    z_invoice_date = fields.Date(string='Invoice Date')
    z_amount_total = fields.Float(string='Amount Total', digits=(12, 2), required=True)
    z_state = fields.Char(string='Status')
