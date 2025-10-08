from odoo import models, fields, api, _
from odoo.exceptions import AccessError, ValidationError, UserError
from psycopg2.extras import RealDictCursor
import psycopg2
from datetime import datetime, date, timedelta


class ProjectProject(models.Model):

    _inherit = ["project.project"]

    @api.depends(
        'z_task_ids.z_actual_start_date',
        'z_task_ids.z_actual_end_date',
        'z_task_ids.timesheet_ids',
        'z_task_ids.z_project_task_state'
    )
    def _getProjectInfo(self):
        for this in self:
            parent_task = self.env['project.task'].sudo().search([('project_id', '=', this.id),('parent_id', '=', False)], order='id asc')
            all_task = self.env['project.task'].sudo().search([('project_id', '=', this.id)], order='id asc')
            actual_start = self.env['project.task'].sudo().search([('project_id', '=', this.id)], order='z_actual_start_date asc', limit=1)
            actual_end = self.env['project.task'].sudo().search([('project_id', '=', this.id)], order='z_actual_end_date desc', limit=1)
            actual_mandays = sum(parent_task.mapped('z_actual_budget_mandays'))
            this.z_task_ids = [(6, 0, all_task.ids)]
            this.z_actual_start_date = actual_start.z_actual_start_date if actual_start else False
            this.z_actual_end_date = actual_end.z_actual_end_date if actual_end else False
            this.z_actual_budget_mandays = actual_mandays
            progress = 0
            if parent_task:
                progress_avg = 100 / len(parent_task)
                for x in parent_task:
                    progress += ((progress_avg / 100 * x.z_progress_project / 100) * 100)
            this.z_progress_project = progress

    @api.onchange('z_group_type_project')
    def onchange_group(self):
        for this in self:
            this.z_type_in_project = False
            this.z_type_non_project = False

    @api.depends(
        'z_project_teams2_ids',
        'z_project_teams2_ids.z_employee_id',
        'task_ids',
    )
    def _getProjectTeams(self):
        for this in self:
            project_team = this.z_project_teams2_ids.mapped('z_project_teams_employee_id').ids
            this.z_project_teams_ids = [(6, 0, project_team)]

    # override
    name = fields.Char("Project Code", index='trigram', required=True, tracking=True, translate=True,default_export_compatible=True)
    label_tasks = fields.Char(string='Name of The Projects', default=lambda s: s.env._(''), translate=True,help="Name used to refer to the tasks of your project e.g. tasks, tickets, sprints, etc...")
    # added
    z_project_status = fields.Selection([
        ('new', 'New'),
        ('waiting', 'Waiting'),
        ('confirm', 'Confirmed'),
        ('sales_dir_to_approve', 'Sales Dir to Approve'),
        ('head_pmo_to_approve', 'Head of PMO to Approve'),
        ('operation', 'Operation'),
        ('budget_approve', 'Budget Approve'),
        ('finance_dir_to_approve', 'Finance Dir to Approve'),
        ('in_progress', 'In Progress'),
        ('full_approve', 'Fully Approved'),
        ('hold', 'On Hold'),
        ('solved', 'Solved'),
        ('closed', 'Closed'),
        ('failed', 'Failed'),
        ('cancel', 'Cancelled'),
    ],string='Project Status',default='new')
    z_group_type_project = fields.Selection([
        ('project', 'Project'),
        ('non_project', 'Non Project')
    ],string='Group')
    z_type_in_project = fields.Selection([
        ('delivery', 'Delivery'),
        ('maintenance', 'Maintenance')
    ],string='Type Of Project')
    z_type_non_project = fields.Selection([
        ('others', 'Others'),
        ('ticket', 'Ticket')
    ],string='Type Of Non Project')
    z_task_ids = fields.Many2many('project.task',relation='project_project_task_ids_rel',string='Task',compute=_getProjectInfo,store=False)
    z_actual_start_date = fields.Date(string='Actual Start Date',compute=_getProjectInfo,store=True)
    z_actual_end_date = fields.Date(string='Actual End Date',compute=_getProjectInfo,store=True)
    z_mandays_budget = fields.Float(string="Budget Mandays")
    z_actual_budget_mandays = fields.Float(string="Actual Mandays",compute=_getProjectInfo,store=True)
    z_value_project = fields.Float(string="Project Value (Rp.)")
    z_progress_project = fields.Float(string="Progress",compute=_getProjectInfo,store=True)
    z_program_name_ids = fields.One2many('project.project.program.name','z_project_id',string='Program Name')
    z_invoice_plan_ids = fields.One2many('project.project.invoice.plan','z_project_id',string='Invoice Plan')
    z_requestor_id = fields.Many2one('hr.employee',string='Requestor')
    z_ticket_admin_id = fields.Many2one('hr.employee',string='Ticket Admin')
    z_request_date = fields.Date(string='Request Date')
    z_attachment_ids = fields.Many2many('ir.attachment',relation='project_project_attachment_ids_rel',string='Attachments')
    z_project_manager_ids = fields.Many2many('hr.employee',relation='project_project_project_manager_ids_rel',column1='project_project_id',column2='hr_employee_id',string="Project Manager")
    z_project_teams2_ids = fields.One2many('project.project.project.teams','z_project_teams_project_id',string="Project Manager")
    z_project_teams_ids = fields.Many2many('hr.employee',relation='project_project_project_teams_ids_rel',string='Project Teams',compute='_getProjectTeams',store=True)
    z_integrate_ok = fields.Boolean(string='Integrate',default=False,tracking=True)

    def action_confirm(self):
        if self.z_group_type_project == 'project':
            if self.z_project_status == 'new':
                self.z_project_status = 'waiting'
            elif self.z_project_status == 'waiting':
                self.z_project_status = 'confirm'
            elif self.z_project_status == 'confirm':
                self.z_project_status = 'sales_dir_to_approve'
            elif self.z_project_status == 'sales_dir_to_approve':
                self.z_project_status = 'head_pmo_to_approve'
            elif self.z_project_status == 'head_pmo_to_approve':
                self.z_project_status = 'operation'
            elif self.z_project_status == 'operation':
                self.z_project_status = 'budget_approve'
            elif self.z_project_status == 'budget_approve':
                self.z_project_status = 'finance_dir_to_approve'
            elif self.z_project_status == 'finance_dir_to_approve':
                self.z_project_status = 'full_approve'
            elif self.z_project_status == 'full_approve':
                self.z_project_status = 'closed'
        elif self.z_group_type_project == 'non_project' and self.z_type_non_project == 'ticket':
            if self.z_project_status == 'new':
                self.z_project_status = 'in_progress'
            elif self.z_project_status == 'hold':
                self.z_project_status = 'in_progress'
            elif self.z_project_status == 'in_progress':
                self.z_project_status = 'solved'
        self._send_reminder_open_composer_project_force()

    def action_failed(self):
        self.z_project_status = 'failed'

    def action_set_to_draft(self):
        self.z_project_status = 'new'

    def action_create_subtask(self):
        self.env['project.task'].create({
            'parent_id': False,
            'partner_id': self.partner_id.id,
            'project_id': self.id,
            'z_head_assignes_ids': [(6, 0, [])],
            'z_member_assignes_ids': [(6, 0, [])],
            'tag_ids': [(6, 0, self.tag_ids.ids)],
            'z_planned_start_date': datetime.now(),
            'z_planned_end_date': datetime.now(),
            'date_deadline': datetime.now(),
        })
        self.action_view_tasks()

    def action_view_tasks(self):
        task_ids = self.task_ids.filtered(lambda x: not x.parent_id)
        return {
            "name": _("Tasks"),
            "type": "ir.actions.act_window",
            "res_model": "project.task",
            "view_mode": "list,form",
            "domain": [('id', 'in', task_ids.ids)],
            "context": {'active_model': 'project.project','default_project_id': self.id},
            'views': [
                [self.env.ref('z_project.z_project_task_list').id, 'list'],
                [self.env.ref('z_project.z_project_task_form_inherit_project').id, 'form'],
            ],
        }

    def send_reminder_mail_project(self):
        template = self.env.ref('z_project.z_mail_template_project_project', raise_if_not_found=False)
        if not template:
            raise ValidationError('Projects mail template kosong. Silahkan cek kembali.')
        return self._send_reminder_open_composer_project(template.id)

    def _send_reminder_open_composer_project(self, template_id):
        self.ensure_one()
        compose_form_id = self.env['ir.model.data']._xmlid_lookup('mail.email_compose_message_wizard_form')[1]
        ctx = dict(self.env.context or {})
        ctx.update({
            'default_model': 'project.project',
            'default_res_ids': self.ids,
            'default_template_id': template_id,
            'default_composition_mode': 'comment',
            'default_email_layout_xmlid': "mail.mail_notification_layout_with_responsible_signature",
            'force_email': True,
            'mark_rfq_as_sent': True,
        })
        lang = self.env.context.get('lang')
        if {'default_template_id', 'default_model', 'default_res_id'} <= ctx.keys():
            template = self.env['mail.template'].browse(ctx['default_template_id'])
            if template and template.lang:
                lang = template._render_lang([ctx['default_res_id']])[ctx['default_res_id']]
        self = self.with_context(lang=lang)
        ctx['model_description'] = _('Projects')
        return {
            'name': _('Compose Email'),
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'res_model': 'mail.compose.message',
            'views': [(compose_form_id, 'form')],
            'view_id': compose_form_id,
            'target': 'new',
            'context': ctx,
        }

    def _send_reminder_open_composer_project_force(self):
        template_id = self.env.ref('z_project.z_mail_template_project_project', raise_if_not_found=False)
        if not template_id:
            return False
        mail_values = {
            'model': 'project.project',
            'res_ids': self.ids,
            'template_id': template_id.id,
            'composition_mode': 'comment',
        }
        mail = self.env['mail.compose.message'].create(mail_values)
        mail.action_send_mail()
        mail_ids = self.env['mail.mail'].sudo().search([('model', '=', 'project.project'), ('res_id', '=', self.id)], order='id desc', limit=1)
        mail_ids.send()

    def action_sync_old_system(self):
        url = self.env['ir.config_parameter'].sudo().get_param('projects.integration.base.url')
        db = self.env['ir.config_parameter'].sudo().get_param('projects.integration.db.name')
        username = self.env['ir.config_parameter'].sudo().get_param('projects.integration.username')
        password = self.env['ir.config_parameter'].sudo().get_param('projects.integration.password')
        if not all([url, db, username, password]):
            raise ValidationError('System information incomplete / missing required info.')
        try:
            conn = psycopg2.connect(
                host=url,
                port="5432",
                dbname=db,
                user=username,
                password=password
            )
            print("✅ Connected to PSQL!")
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT
                    id,
                    project_code,
                    project_name,
                    customer,
                    project_value,
                    total_cost_plan,
                    margin,
                    gross_profit_plan,
                    total_realized,
                    margin_realized,
                    gross_profit_actual,
                    prospect_status,
                    project_status,
                    maintenance_status,
                    version
                FROM v_fsm_pcb
            """)
            rows = cur.fetchall()
            for row in rows:
                partner = False
                if row['customer']:
                    partner = self.env['res.partner'].sudo().search([
                        ('name', '=', row['customer'])
                    ], order='id desc', limit=1)
                    partner_vals = {
                        'name': row['customer'],
                        'is_company': True,
                        'z_integrate_ok': True,
                    }
                    if partner and not partner.z_integrate_ok:
                        partner.sudo().write(partner_vals)
                    elif not partner:
                        partner = self.env['res.partner'].sudo().create(partner_vals)
                if row['project_code'] and row['project_name']:
                    domain_project = [
                        ('name', '=', row['project_code']),
                        ('label_tasks', '=', row['project_name']),
                    ]
                    if partner:
                        domain_project.append(('partner_id', '=', partner.id))
                    project = self.env['project.project'].sudo().search(domain_project, order='id desc', limit=1)
                    project_vals = {
                        'name': row['project_code'],
                        'label_tasks': row['project_name'],
                        'z_value_project': row['project_value'],
                        'z_integrate_ok': True,
                    }
                    if partner:
                        project_vals['partner_id'] = partner.id
                    if 'IP' in row['project_code']:
                        project_vals['z_group_type_project'] = 'project'
                        project_vals['z_type_in_project'] = 'delivery'
                    if 'MT' in row['project_code']:
                        project_vals['z_group_type_project'] = 'project'
                        project_vals['z_type_in_project'] = 'maintenance'
                    if project:
                        project.sudo().write(project_vals)
                    elif not project:
                        self.env['project.project'].sudo().create(project_vals)
            cur.execute("""
                SELECT
                    id,
                    name,
                    work_email,
                    department_id,
                    job_id,
                    employee_id,
                    deptname,
                    deptcompletename,
                    jobname
                FROM v_fsm_emp_list
            """)
            rows = cur.fetchall()
            for row in rows:
                job = False
                department = False
                if row['jobname']:
                    job = self.env['hr.job'].sudo().search([
                        ('name', '=', row['jobname'])
                    ], order='id desc', limit=1)
                    job_vals = {
                        'name': row['jobname'],
                        'z_integrate_ok': True
                    }
                    if job and not job.z_integrate_ok:
                        job.sudo().write(job_vals)
                    elif not job:
                        job = self.env['hr.job'].sudo().create(job_vals)
                if row['deptname']:
                    department = self.env['hr.department'].sudo().search([
                        ('name', '=', row['deptname'])
                    ], order='id desc', limit=1)
                    department_vals = {
                        'name': row['deptname'],
                        'z_integrate_ok': True
                    }
                    if department and not department.z_integrate_ok:
                        department.sudo().write(department_vals)
                    elif not department:
                        department = self.env['hr.department'].sudo().create(department_vals)
                if row['name']:
                    employee = self.env['hr.employee'].sudo().search([
                        ('name', '=', row['name'])
                    ], order='id desc', limit=1)
                    employee_vals = {
                        'name': row['name'],
                        'work_email': row['work_email'],
                        'job_id': job.id if job else False,
                        'department_id': department.id if department else False,
                        'z_integrate_ok': True
                    }
                    if employee and not employee.z_integrate_ok:
                        employee.sudo().write(employee_vals)
                    elif not employee:
                        self.env['hr.employee'].sudo().create(employee_vals)
            cur.close()
            conn.close()
        except Exception as e:
            print("❌ Connection error:", e)


class ProjectProjectProjectTeams(models.Model):

    _name = "project.project.project.teams"
    _description = "Project Teams"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _rec_name = "z_project_teams_employee_id"
    _order = "z_sequence asc"

    @api.depends(
        'z_project_teams_job2_id',
        'z_project_teams_job2_bobot',
        'z_project_teams_project_id.z_project_teams2_ids',
    )
    def _getBobot(self):
        for this in self:
            bobot = this.z_project_teams_job2_bobot
            project_teams = this.z_project_teams_project_id.z_project_teams2_ids.filtered(lambda x: x.z_project_teams_job2_id.id == this.z_project_teams_job2_id.id)
            if project_teams:
                bobot = bobot / len(project_teams)
            this.z_project_teams_bobot = bobot

    z_sequence = fields.Integer(string='Sequence',default=0)
    z_project_teams_project_id = fields.Many2one('project.project', string='Project', ondelete='cascade')
    z_project_teams_employee_id = fields.Many2one('hr.employee', string='Employee')
    z_project_teams_job_id = fields.Many2one('hr.job', string='Job Position', related='z_project_teams_employee_id.job_id',store=True)
    z_project_teams_job2_id = fields.Many2one('hr.job', string='Job Position (Current)')
    z_project_teams_job2_bobot = fields.Float(string='Bobot (%)', related='z_project_teams_job2_id.z_bobot', store=True)
    z_project_teams_bobot = fields.Float(string='Bobot (%)', compute=_getBobot, store=True)
    # not used
    z_project_id = fields.Many2one('project.project',string='Project')
    z_employee_id = fields.Many2one('hr.employee',string='Employee')
    z_job_id = fields.Many2one('hr.job',string='Job Position',related='z_employee_id.job_id',store=True)
    z_job2_id = fields.Many2one('hr.job',string='Job Position (Current)')
    z_job2_bobot = fields.Float(string='Bobot (%)',related='z_job2_id.z_bobot',store=True)
    z_bobot = fields.Float(string='Bobot (%)',compute=_getBobot,store=True)


class ProjectProjectProgramName(models.Model):

    _name = "project.project.program.name"
    _description = "Program Name"
    _inherit = ["mail.thread","mail.activity.mixin"]
    _rec_name = "z_name"
    _order = "id asc"

    z_project_id = fields.Many2one('project.project',string='Project',ondelete='cascade')
    z_name = fields.Char(string='Pogram Name')


class ProjectProjectInvoicePlan(models.Model):

    _name = "project.project.invoice.plan"
    _description = "Invoice Plan"
    _inherit = ["mail.thread","mail.activity.mixin"]
    _rec_name = "z_name"
    _order = "id asc"

    z_project_id = fields.Many2one('project.project',string='Project',ondelete='cascade')
    z_name = fields.Char(string='No. Invoice')
    z_description = fields.Char(string='Invoice Description')
    z_date = fields.Date(string='Invoice Date')
    z_amount_total = fields.Float(string='Amount Total')
    z_state = fields.Char(string='Status')
