from odoo import http, fields, tools
from odoo.http import request, Response
from odoo.addons.portal.controllers.portal import pager
from datetime import datetime
import pytz
import json
import logging
import re
from html import unescape
import base64
from odoo.osv import expression
from urllib.parse import urlencode

_logger = logging.getLogger(__name__)
WIB = pytz.timezone('Asia/Jakarta')  # default fallback


def _user_tz():
    try:
        tzname = (request.env.context.get('tz') or request.env.user.tz) or 'Asia/Jakarta'
        return pytz.timezone(tzname)
    except Exception:
        return WIB


def _parse_datetime_to_utc(val):
    if not val:
        return False
    s = (val or '').strip()
    if not s:
        return False
    try:
        # Normalisasi AM/PM agar bisa diparse %p
        s_am = s.replace(' am', 'AM').replace(' pm', 'PM').replace(' AM', 'AM').replace(' PM', 'PM')
        s_am = s_am.replace('am', 'AM').replace('pm', 'PM')
        fmts_24 = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"]
        fmts_12 = ["%Y-%m-%dT%I:%M%p", "%Y-%m-%d %I:%M%p", "%Y-%m-%dT%I:%M:%S%p", "%Y-%m-%d %I:%M:%S%p"]
        dt_local = None
        for f in fmts_24:
            try:
                dt_local = datetime.strptime(s_am, f)
                break
            except Exception:
                continue
        if dt_local is None:
            for f in fmts_12:
                try:
                    dt_local = datetime.strptime(s_am, f)
                    break
                except Exception:
                    continue
        if dt_local is None:
            return False
        tz = _user_tz()
        localized = tz.localize(dt_local)
        utc_dt = localized.astimezone(pytz.utc)
        return utc_dt.replace(tzinfo=None)
    except Exception:
        return False


def _format_datetime_to_user_tz(dt_val):
    if not dt_val:
        return ''
    tz = _user_tz()
    if dt_val.tzinfo:
        dt_val = dt_val.astimezone(tz)
    else:
        dt_val = pytz.utc.localize(dt_val).astimezone(tz)
    # 24 jam untuk input datetime-local
    return dt_val.strftime('%Y-%m-%dT%H:%M')


def _to_wib(dt_val, with_seconds=False):
    # NOTE: tetap pakai nama lama, tetapi menggunakan timezone user (WIB/WITA/WIT) secara dinamis
    if not dt_val:
        return ''
    tz = _user_tz()
    if dt_val.tzinfo:
        d = dt_val.astimezone(tz)
    else:
        d = pytz.utc.localize(dt_val).astimezone(tz)
    fmt = '%d/%m/%Y %H:%M:%S' if with_seconds else '%d/%m/%Y %H:%M'
    return d.strftime(fmt)


class PortalProjectControllers(http.Controller):

    # ================= MASTER TASK HELPERS =================
    def _find_root_master(self, rec):
        """Naik ke master paling atas (z_parent_id = False)."""
        if not rec:
            return rec
        cur = rec
        while getattr(cur, 'z_parent_id', False):
            cur = cur.z_parent_id
        return cur

    def _gather_master_tree(self, root):
        """(Masih tersedia jika dibutuhkan untuk mode lama)"""
        Master = request.env['task.master'].sudo()
        if not root:
            return Master.browse()
        result = root
        stack = [root]
        while stack:
            node = stack.pop()
            children = Master.search([('z_parent_id', '=', node.id)])
            for ch in children:
                if ch not in result:
                    result |= ch
                    stack.append(ch)
        return result

    # ---------------- ROLE FLAGS (UPDATED) ----------------
    def _role_flags(self):
        cache_key = '_z_portal_role_flags_v12_unified'
        if hasattr(request, cache_key):
            return getattr(request, cache_key)

        user = request.env.user
        is_internal_super = user.has_group('base.group_user') and not user.has_group('base.group_portal')
        group_rec = getattr(user, 'z_project_group_id', False)
        group_name = group_rec.name if group_rec else ''

        name_map = {
            'Projects: Administrator': 'project_admin',
            'Projects: Project Manager': 'project_manager',
            'Projects: Head': 'project_head',
            'Projects: Staff': 'project_engineer',
            'Projects: Staff Engineer': 'project_engineer',
            'Projects: Staff Support': 'project_delivery_support',
            'Projects: Readonly': 'project_readonly',
            'Projects: Head Engineer': 'project_head',
            'Projects: Delivery Support': 'project_delivery_support',
        }
        ga = name_map.get(group_name)

        if not ga and group_rec:
            try:
                xmlid_full = group_rec.get_external_id().get(group_rec.id)
            except Exception:
                xmlid_full = None
            if xmlid_full:
                suffix_map = {
                    '_project_manager': 'project_manager',
                    '_lead': 'project_head',
                    '_head_engineer': 'project_head',
                    '_user': 'project_engineer',
                    '_readonly': 'project_delivery_support',
                }
                for suf, code in suffix_map.items():
                    if xmlid_full.endswith(suf):
                        ga = code
                        break

        if is_internal_super and not ga:
            ga = 'project_admin'

        is_admin = (ga == 'project_admin')
        is_pm = (ga == 'project_manager')
        is_head = (ga == 'project_head')
        is_engineer = (ga == 'project_engineer')
        is_delivery = (ga == 'project_delivery_support')
        is_readonly = (ga == 'project_readonly')

        is_head_engineer = is_head

        # Permissions
        can_approve_timesheet = (is_admin or is_pm or is_head)
        can_submit_task = (is_admin or is_pm or is_head or is_engineer)
        can_approve_task = (is_admin or is_pm)
        can_reject_task = (is_admin or is_pm or is_head)
        can_finish_task = (is_admin or is_pm)
        can_update_task = (is_admin or is_pm or is_head) and not is_readonly
        can_view_project_page = (is_admin or is_pm)

        flags = {
            'groups_access': ga,
            'is_internal_super': is_internal_super,

            'is_admin': is_admin,
            'is_pm': (is_pm or is_admin),

            'is_head': is_head,
            'is_head_engineer': is_head_engineer,

            'is_engineer': is_engineer,
            'is_delivery_support': is_delivery,

            'is_readonly_user': is_readonly,

            'is_support': is_delivery or is_readonly,
            'is_staff': is_engineer or is_delivery,

            'can_create_task': (is_admin or is_pm),
            'can_update_task': can_update_task,
            'can_delete_task': (is_admin or is_pm) and not is_readonly,
            'can_submit_task': can_submit_task and not is_readonly,
            'can_approve_task': can_approve_task and not is_readonly,
            'can_reject_task': can_reject_task and not is_readonly,
            'can_finish_task': can_finish_task and not is_readonly,
            'can_create_subtask': (is_admin or is_pm or is_head) and not is_readonly,
            'can_delete_subtask': (is_admin or is_pm) and not is_readonly,

            'can_approve_timesheet': can_approve_timesheet,
            'can_delete_timesheet': can_approve_timesheet and not is_readonly,

            # Engineer/Delivery/Readonly dibatasi melihat timesheet sendiri,
            'restrict_timesheet_to_self': (is_engineer or is_delivery or is_readonly) and not (
                        is_admin or is_pm or is_head),
            'show_tab_invoice_plan': (is_admin or is_pm or is_head or is_delivery or is_readonly or is_internal_super),
            'can_edit_invoice_plan': (is_admin or is_pm) and not is_readonly,

            'show_tab_description': (is_admin or is_pm or is_head) and not is_readonly,
            'show_tab_subtasks': (is_admin or is_pm or is_head) and not is_readonly,
            'show_metrics': (is_admin or is_pm or is_head) and not is_readonly,
            'show_quality': (is_admin or is_pm or is_head) and not is_readonly,

            'can_view_project_page': can_view_project_page,
        }
        setattr(request, cache_key, flags)
        return flags

    # ---------------- COMMON DATA ----------------
    def _common_data(self):
        env = request.env
        return {
            'master_tasks': env['task.master'].sudo().search([]),
            'employees': env['hr.employee'].sudo().search([]),
            'technologies': env['technology.used'].sudo().search([]),
            'severities': env['severity.master'].sudo().search([]),
            'regionals': env['area.regional'].sudo().search([]),
        }

    # ---------------- PROJECT TEAMS EMPLOYEES ----------------
    def _get_project_team_employees(self, project):
        Employee = request.env['hr.employee'].sudo()
        if not project:
            return Employee.browse()
        team_lines = getattr(project, 'z_project_teams2_ids', False) or getattr(project, 'z_project_teams_ids', False)
        if not team_lines:
            return Employee.browse()
        candidate_fields = ['z_project_teams_employee_id', 'employee_id', 'z_employee_id']
        employees = Employee.browse()
        for f in candidate_fields:
            if any(hasattr(line, f) for line in team_lines):
                employees |= team_lines.mapped(f)
        return employees.exists()

    # ---------------- UTIL TEXT ----------------
    def _convert_html_to_text(self, html_content):
        if not html_content:
            return ''
        txt = re.sub(r'<br\s*/?>', '\n', html_content)
        txt = re.sub(r'</p\s*>', '\n', txt)
        txt = re.sub(r'<[^>]+>', '', txt)
        return unescape("\n".join([l.rstrip() for l in txt.splitlines()]).strip())

    def _extract_employee_ids(self, record):
        emp_ids = set()
        emp = getattr(record, 'z_employee_id', False)
        if emp:
            emp_ids.add(emp.id)
        ts = getattr(record, 'z_timesheet_id', False)
        if ts and getattr(ts, 'employee_id', False):
            emp_ids.add(ts.employee_id.id)
        lines = getattr(record, 'z_line_ids', False)
        if lines:
            for line in lines:
                le = getattr(line, 'z_employee_id', False) or getattr(line, 'employee_id', False)
                if le:
                    emp_ids.add(le.id)
        return emp_ids

    def _filter_requests_by_role(self, recs, task, flags, employee):
        # PM/Admin/Head Engineer melihat semua
        if flags['can_approve_timesheet']:
            return recs

        # Khusus Others: izinkan atasan langsung melihat request bawahannya
        recs_manager = recs.browse()
        if task.z_type_non_project == 'others' and employee:
            recs_manager = recs.filtered(
                lambda r:
                (r.z_employee_id and r.z_employee_id.parent_id and r.z_employee_id.parent_id.id == employee.id)
                or
                (r.z_timesheet_id and r.z_timesheet_id.employee_id
                 and r.z_timesheet_id.employee_id.parent_id
                 and r.z_timesheet_id.employee_id.parent_id.id == employee.id)
            )

        # Head Engineer: yang terkait assignee task
        assignees = (task.z_head_assignes_ids | task.z_member_assignes_ids)
        recs_headeng = recs.filtered(lambda r: bool(self._extract_employee_ids(r) & set(assignees.ids))) if flags[
            'is_head_engineer'] else recs.browse()

        # Engineer/Delivery/Readonly: hanya milik sendiri
        recs_self = recs.filtered(lambda r: employee.id in self._extract_employee_ids(r)) if (flags['is_engineer'] or
                                                                                              flags[
                                                                                                  'is_support']) and employee else recs.browse()

        return (recs_manager | recs_headeng | recs_self)

    def _compute_depth_map(self, tasks):
        depth_map = {}
        for t in tasks:
            depth = 0
            cur = t
            while cur.parent_id:
                depth += 1
                cur = cur.parent_id
            depth_map[t.id] = depth
        return depth_map

    def _order_parent_child(self, tasks, latest_first=False):
        """Urutkan parent → child. Default: terlama (id asc) untuk parent & child."""
        if not tasks:
            return tasks
        Task = request.env['project.task'].sudo()
        ordered = Task.browse()
        if latest_first:
            parents = tasks.filtered(lambda t: not t.parent_id).sorted(key=lambda r: r.id, reverse=True)
        else:
            parents = tasks.filtered(lambda t: not t.parent_id).sorted(key=lambda r: r.id)  # id asc = terlama

        def add_branch(p):
            nonlocal ordered
            ordered |= p
            if latest_first:
                children = tasks.filtered(lambda c: c.parent_id.id == p.id).sorted(key=lambda r: r.id, reverse=True)
            else:
                children = tasks.filtered(lambda c: c.parent_id.id == p.id).sorted(key=lambda r: r.id)  # id asc
            for ch in children:
                add_branch(ch)

        for p in parents:
            add_branch(p)

        # Orphans (parent di luar set) ikut dimasukkan supaya tampil
        if latest_first:
            orphans = tasks.filtered(lambda t: t.parent_id and t.parent_id not in tasks).sorted(key=lambda r: r.id,
                                                                                                reverse=True)
        else:
            orphans = tasks.filtered(lambda t: t.parent_id and t.parent_id not in tasks).sorted(key=lambda r: r.id)
        for o in orphans:
            if o not in ordered:
                add_branch(o)
        return ordered

    def _get_running_task_ids(self, task_ids):
        """Kembalikan set id task yang punya timesheet berjalan (end_date False)."""
        try:
            if not task_ids:
                return set()
            AAL = request.env['account.analytic.line'].sudo()
            open_lines = AAL.search([
                ('task_id', 'in', list(task_ids)),
                ('z_timesheet_end_date', '=', False),
            ])
            return set(open_lines.mapped('task_id').ids)
        except Exception:
            return set()

    # ---------------- AUX AFTER CREATE ----------------
    def _finalize_portal_task_after_create(self, task, forced_project_id=None):
        if not task:
            return
        try:
            if forced_project_id and (not task.project_id or task.project_id.id != forced_project_id):
                prj = request.env['project.project'].sudo().browse(forced_project_id).exists()
                if prj:
                    task.write({'project_id': prj.id})
            if task.project_id and task.project_id.partner_id and \
                    (not task.partner_id or task.partner_id.id != task.project_id.partner_id.id):
                task.write({'partner_id': task.project_id.partner_id.id})

            name_ok = True
            if task.parent_id:
                parent_code = task.parent_id.name or ''
                if not re.fullmatch(rf'{re.escape(parent_code)}\.\d{{2}}', task.name or ''):
                    name_ok = False
            else:
                if not re.fullmatch(r'T-\d{2}', task.name or ''):
                    name_ok = False
            if not name_ok:
                if task.parent_id:
                    task.generate_sequence_name()
                else:
                    task.generate_project_sequence_name()
        except Exception as e:
            _logger.error("Finalize portal task error: %s", e)

    # ---------------- DOCUMENT HELPERS ----------------
    def _create_document_from_attachment(self, attachment, task):
        try:
            Docs = request.env['documents.document'].sudo()
        except Exception:
            return
        if not hasattr(Docs, 'create'):
            return
        folder = None
        try:
            folder = request.env.ref('documents.documents_finance_folder', raise_if_not_found=False)
        except Exception:
            folder = None
        vals = {
            'name': attachment.name,
            'attachment_id': attachment.id,
            'folder_id': folder and folder.id or False,
            'owner_id': request.env.user.id,
            'partner_id': task.partner_id.id if task.partner_id else False,
            'res_model': 'project.task',
            'res_id': task.id,
        }
        try:
            Docs.create(vals)
        except Exception as e:
            _logger.error("Create document from attachment failed: %s", e)

    @http.route('/portal/project/<int:project_id>/info', type='http', auth='user', methods=['GET'])
    def portal_project_info(self, project_id):
        try:
            prj = request.env['project.project'].sudo().browse(project_id).exists()
            if not prj:
                return Response(json.dumps({'error': 'Project not found'}), content_type='application/json')
            info = {
                'id': prj.id,
                'name': prj.name or '',
                'label_tasks': prj.label_tasks or '',
                'partner_id': prj.partner_id.id if prj.partner_id else False,
                'partner_name': prj.partner_id.name if prj.partner_id else '',
                'z_type_in_project': prj.z_type_in_project or '',
                'z_group_type_project': prj.z_group_type_project or '',
                'z_type_non_project': prj.z_type_non_project or '',
            }
            return Response(json.dumps(info), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    @http.route('/portal/project/<int:project_id>/team', type='http', auth='user', methods=['GET'])
    def portal_project_team(self, project_id):
        try:
            prj = request.env['project.project'].sudo().browse(project_id).exists()
            if not prj:
                return Response(json.dumps({'success': False, 'error': 'Project not found'}),
                                content_type='application/json')
            emps = self._get_project_team_employees(prj) or request.env['hr.employee'].sudo().browse()
            data = [{'id': e.id, 'name': e.name or ''} for e in emps]
            return Response(json.dumps({'success': True, 'data': data}), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- ROUTE ----------------
    @http.route([
        '/portal/tasks',
        '/portal/tasks/page/<int:page>',
        '/portal/tasks/delete/<int:task_id>',
        '/portal/task/<int:task_id>',
        '/portal/tasks/<int:task_id>',
        '/portal/tasks/parent/<int:parent_id>',
        '/portal/tasks/parent/<int:parent_id>/page/<int:page>',
        '/portal/tasks/parent/<int:parent_id>/new',
    ], type='http', auth='user', website=True, methods=['GET', 'POST'])
    def portal_tasks(self, task_id=None, page=1, search='', sortby='terlama',
                     parent_id=None, project_id=None, groupby='', **kw):

        flags = self._role_flags()
        Task = request.env['project.task'].sudo()
        common = self._common_data()
        view_type = kw.get('view_type')

        def _to_int(v):
            try:
                if v is None or v is False or v == '':
                    return None
                return int(v)
            except Exception:
                return None

        # NORMALIZE: always coerce IDs from either route args or query string to integers
        parent_id = _to_int(parent_id if parent_id is not None else kw.get('parent_id'))
        project_id = _to_int(project_id if project_id is not None else kw.get('project_id'))
        task_id = _to_int(task_id)

        # Persist & fallback project filter via session (normalize types)
        try:
            if project_id and not (flags.get('is_pm') or flags.get('is_head_engineer') or flags.get('is_engineer')):
                request.session['portal_last_project_id'] = int(project_id)
            elif not parent_id and not task_id and 'project_id' not in request.httprequest.args:
                if not (flags.get('is_pm') or flags.get('is_head_engineer') or flags.get('is_engineer')):
                    last_pid_raw = request.session.get('portal_last_project_id')
                    last_pid = _to_int(last_pid_raw)
                    if last_pid:
                        project_id = last_pid
                        kw['project_id'] = last_pid
        except Exception:
            pass

        if request.httprequest.method == 'GET' \
                and not parent_id and not task_id and not kw.get('mode') and view_type != 'form':
            if (flags.get('is_head_engineer') or flags.get('is_engineer')) and (not groupby):
                return request.redirect('/portal/tasks?groupby=project_id')

        parent_task = Task.browse(parent_id).exists() if parent_id else False
        project = request.env['project.project'].sudo().browse(project_id).exists() if project_id else False

        # Redirect legacy singular
        if request.httprequest.method == 'GET' and task_id and '/portal/task/' in request.httprequest.path:
            if kw.get('mode') == 'edit':
                return request.redirect(f'/portal/tasks/{task_id}?mode=edit')
            if view_type == 'form':
                return request.redirect(f'/portal/tasks/{task_id}?view_type=form')
            return request.redirect(f'/portal/tasks/{task_id}')

        searchbar_sortings = {
            'terbaru': {'label': 'Terbaru', 'order': 'id desc'},
            'terlama': {'label': 'Terlama', 'order': 'id asc'},
            'name': {'label': 'Task Code', 'order': 'name asc'},
            'project': {'label': 'Project', 'order': 'project_id asc'},
            'status': {'label': 'Status', 'order': 'z_project_task_state asc'},
            'customer': {'label': 'Customer', 'order': 'partner_id asc'},
        }
        searchbar_groupings = {
            '': {'label': 'None'},
            'project_id': {'label': 'Project'},
            'partner_id': {'label': 'Customer'},
            'z_project_task_state': {'label': 'Status'},
            'z_master_task_id': {'label': 'Master Task'},
            'z_technology_id': {'label': 'Technology'},
            'z_severity_id': {'label': 'Severity'},
        }
        order = searchbar_sortings.get(sortby, searchbar_sortings['terlama'])['order']

        # CREATE SECURITY
        if kw.get('mode') == 'new' and not flags['can_create_task']:
            return request.redirect('/portal/tasks?error=Not allowed')

        # DELETE
        if request.httprequest.method == 'POST' and task_id and '/delete/' in request.httprequest.path:
            if not flags['can_delete_task']:
                return request.redirect('/portal/tasks?error=Not allowed')
            tdel = Task.browse(task_id).exists()
            if tdel:
                tdel.unlink()
            return request.redirect('/portal/tasks?success=Task deleted')

        # UPDATE (header)
        if request.httprequest.method == 'POST' and task_id and '/delete/' not in request.httprequest.path \
                and (kw.get('mode') == 'edit' or view_type == 'form') \
                and not kw.get('_workflow') and not kw.get('function'):
            t = Task.browse(task_id).exists()
            if not t:
                return request.redirect('/portal/tasks?error=Task tidak ditemukan')
            if not flags['can_update_task'] and not (
                    flags.get('is_head_engineer') and not (flags.get('is_pm') or flags.get('is_admin'))):
                return request.redirect(f'/portal/tasks/{task_id}?mode=edit&error=Tidak diizinkan untuk mengubah')
            form = request.httprequest.form

            def si(val):
                try:
                    return int(val) if val and str(val).strip() else False
                except:
                    return False

            def sf(val):
                try:
                    return float(val) if val not in (None, '', False) else 0.0
                except:
                    return 0.0

            def clamp01(val):
                return max(0.0, min(100.0, val))

            vals = {}
            self_create = str(form.get('z_master_task_self_create_ok') or '').lower() in ('1', 'true', 'on', 'yes')
            free_text = (form.get('z_master_task_free_text') or '').strip()
            if self_create:
                vals['z_master_task_self_create_ok'] = True
                vals['z_master_task_free_text'] = free_text
                vals['z_master_task_id'] = False
            else:
                vals['z_master_task_self_create_ok'] = False
                if 'z_master_task_id' in form:
                    mt = si(form.get('z_master_task_id'))
                    vals['z_master_task_id'] = mt or False
                if 'z_master_task_free_text' in form and not self_create:
                    vals['z_master_task_free_text'] = ''

            if 'project_id' in form and not t.parent_id:
                pj = si(form.get('project_id'))
                if pj:
                    vals['project_id'] = pj

            if 'z_head_assignes_ids' in form:
                head_ids = form.getlist('z_head_assignes_ids')
                vals['z_head_assignes_ids'] = [(6, 0, [int(x) for x in head_ids if x])]
            if 'z_member_assignes_ids' in form:
                member_ids = form.getlist('z_member_assignes_ids')
                vals['z_member_assignes_ids'] = [(6, 0, [int(x) for x in member_ids if x])]
            if 'tag_ids' in form:
                tag_ids = form.getlist('tag_ids')
                vals['tag_ids'] = [(6, 0, [int(x) for x in tag_ids if x])]
            if 'z_planned_start_date' in form:
                vals['z_planned_start_date'] = form.get('z_planned_start_date') or False
            if 'z_planned_end_date' in form:
                vals['z_planned_end_date'] = form.get('z_planned_end_date') or False
            if 'z_bobot_entry' in form:
                new_entry = clamp01(sf(form.get('z_bobot_entry')))
                vals['z_bobot_entry'] = new_entry
                if t.parent_id:
                    parent_entry = t.parent_id.z_bobot_entry or 0.0
                    vals['z_bobot'] = (new_entry / parent_entry * 100.0) if parent_entry else 0.0
                else:
                    vals['z_bobot'] = new_entry
            if 'z_quality_entry' in form and t.z_project_task_state == 'done':
                vals['z_quality_entry'] = clamp01(sf(form.get('z_quality_entry')))
            if 'z_progress_project_entry' in form and getattr(t, 'z_end_task_ok', False):
                vals['z_progress_project_entry'] = clamp01(sf(form.get('z_progress_project_entry')))
            if 'z_technology_id' in form:
                vals['z_technology_id'] = si(form.get('z_technology_id')) or False
            if 'z_severity_id' in form:
                vals['z_severity_id'] = si(form.get('z_severity_id')) or False
            if 'z_regional_id' in form:
                vals['z_regional_id'] = si(form.get('z_regional_id')) or False
            if 'description' in form:
                raw_desc = form.get('description') or ''
                vals['description'] = raw_desc.replace('\r\n', '\n').replace('\n', '<br/>')

            # Validasi assignee vs project team (tetap)
            proj_for_check = t.project_id
            if proj_for_check and ('z_head_assignes_ids' in vals or 'z_member_assignes_ids' in vals):
                team_ids = set(self._get_project_team_employees(proj_for_check).ids)
                if 'z_head_assignes_ids' in vals and any(e not in team_ids for e in vals['z_head_assignes_ids'][0][2]):
                    return request.redirect(f'/portal/tasks/{t.id}?mode=edit&error=Head bukan anggota Project Teams')
                if 'z_member_assignes_ids' in vals and any(
                        e not in team_ids for e in vals['z_member_assignes_ids'][0][2]):
                    return request.redirect(f'/portal/tasks/{t.id}?mode=edit&error=Member bukan anggota Project Teams')
            elif not proj_for_check and ('z_head_assignes_ids' in vals or 'z_member_assignes_ids' in vals):
                return request.redirect(f'/portal/tasks/{t.id}?mode=edit&error=Task belum terkait project')

            is_head_engineer = flags.get('is_head_engineer')
            is_pm = flags.get('is_pm')
            is_admin = flags.get('is_admin')

            # Head Engineer tanpa PM/Admin: hanya Member saat new/in_progress
            if is_head_engineer and not (is_pm or is_admin):
                if t.z_project_task_state in ('approved1', 'approved2', 'done'):
                    return request.redirect(f'/portal/tasks/{t.id}?mode=edit&error=Tidak diizinkan pada tahap ini')
                allowed = {'z_member_assignes_ids'}
                vals = {k: v for k, v in vals.items() if k in allowed or k == 'description'}

            # PRE-VALIDATION bobot agar tidak error page
            def _sum_sibling_with_new_value(task, new_val):
                if task.parent_id:
                    siblings = task.parent_id.child_ids - task
                    base = sum(siblings.mapped('z_bobot_entry') or [0.0])
                else:
                    siblings = task.project_id.task_ids.filtered(lambda x: not x.parent_id and x.id != task.id)
                    base = sum(siblings.mapped('z_bobot_entry') or [0.0])
                total = round((base + (new_val or 0.0)), 1)
                return total

            if 'z_bobot_entry' in vals:
                total = _sum_sibling_with_new_value(t, vals['z_bobot_entry'])
                if total > 100:
                    return request.redirect(f'/portal/tasks/{t.id}?mode=edit&error=Percentage bobot &gt; 100%')

            if vals:
                task_ids = request.env['project.task'].sudo().search([('id', '=', task_id)], limit=1)
                for x in ['project_id']:
                    vals.pop(x, None)
                if task_ids:
                    task_ids.with_context(from_portal=True, skip_auto_actions=True).write(vals)
                suffix = 'view_type=form' if view_type == 'form' else 'mode=edit'
                return request.redirect(f'/portal/tasks/{task_id}?{suffix}&success=Data berhasil disimpan')

        # WORKFLOW ACTION (REVISED)
        if request.httprequest.method == 'POST' and task_id and kw.get('_workflow') and not kw.get('mode'):
            t = Task.browse(task_id).exists()
            if not t:
                return request.redirect('/portal/tasks?error=Task not found')

            # Default ke 'confirm' bila function tidak dikirim
            action = (kw.get('function') or 'confirm').strip()

            try:
                state = t.z_project_task_state or 'new'
                is_pm = flags.get('is_pm')
                is_admin = flags.get('is_admin')
                is_head_engineer = flags.get('is_head_engineer')

                if action == 'confirm':
                    # Submit / Approve / Done akan ditentukan berdasarkan state
                    if state in ('new', 'in_progress'):
                        if not flags['can_submit_task']:
                            raise Exception("Not allowed to submit")
                    elif state == 'approved1':
                        if not (is_head_engineer or is_pm or is_admin):
                            raise Exception("Not allowed to approve")
                    elif state == 'approved2':
                        if not (is_pm or is_admin):
                            raise Exception("Not allowed to approve to Done")
                    t.action_confirm()

                elif action == 'reject':
                    allowed = False
                    if state == 'approved1' and (is_head_engineer or is_pm or is_admin):
                        allowed = True
                    elif state in ('in_progress', 'approved2') and (is_pm or is_admin):
                        allowed = True
                    if not allowed:
                        raise Exception("Not allowed to reject")
                    reason = (kw.get('reason') or '').strip()
                    if reason:
                        t.write({'z_reason_reject_description': reason})
                    t.action_reject()

                else:
                    raise Exception("Unsupported action")

                suffix = 'view_type=form' if view_type == 'form' else 'mode=edit'
                return request.redirect(f'/portal/tasks/{t.id}?{suffix}&success=Action done')

            except Exception as e:
                suffix = 'view_type=form' if view_type == 'form' else 'mode=edit'
                return request.redirect(f'/portal/tasks/{t.id}?{suffix}&error={tools.html_escape(str(e))}')

        # CREATE
        if request.httprequest.method == 'POST' and not task_id and '/delete/' not in request.httprequest.path:
            if not flags['can_create_task']:
                return request.redirect('/portal/tasks?error=Not allowed')
            try:
                form = request.httprequest.form

                def si2(v):
                    try:
                        return int(v) if v and str(v).strip() else False
                    except:
                        return False

                parent_id_int = si2(form.get('parent_id')) or parent_id
                intended_project_id = si2(form.get('project_id')) or project_id
                prj_rec = request.env['project.project'].sudo().browse(
                    intended_project_id).exists() if intended_project_id else False
                project_is_others = False
                if prj_rec:
                    project_is_others = (
                            prj_rec.z_group_type_project == 'non_project' and prj_rec.z_type_non_project == 'others')

                is_others = (form.get('z_type_non_project') == 'others') or project_is_others

                self_create = str(form.get('z_master_task_self_create_ok') or '').lower() in ('1', 'true', 'on', 'yes')
                free_text = (form.get('z_master_task_free_text') or '').strip()

                vals = {}

                if self_create:
                    vals['z_master_task_self_create_ok'] = True
                    vals['z_master_task_free_text'] = free_text or ('New Task' if not is_others else 'Others Task')
                    vals['z_master_task_id'] = False
                else:
                    vals['z_master_task_self_create_ok'] = False
                    master_id = si2(form.get('z_master_task_id'))
                    if not is_others and not master_id:
                        return request.redirect('/portal/tasks?error=Master Task required')
                    vals['z_master_task_id'] = master_id or False
                    vals['z_master_task_free_text'] = ''

                if is_others:
                    vals['z_type_non_project'] = 'others'
                else:
                    plan_start = form.get('z_planned_start_date')
                    plan_end = form.get('z_planned_end_date')
                    if not plan_start or not plan_end:
                        return request.redirect('/portal/tasks?error=Planned dates required')
                    vals.update({
                        'z_planned_start_date': plan_start,
                        'z_planned_end_date': plan_end,
                    })

                if parent_id_int:
                    parent_task_rec = Task.browse(parent_id_int).exists()
                    if parent_task_rec:
                        vals['parent_id'] = parent_task_rec.id
                        if parent_task_rec.project_id:
                            vals['project_id'] = parent_task_rec.project_id.id
                if not vals.get('project_id') and intended_project_id:
                    prj = request.env['project.project'].sudo().browse(intended_project_id).exists()
                    if prj:
                        vals['project_id'] = prj.id

                if vals.get('project_id'):
                    prj_rec2 = request.env['project.project'].sudo().browse(vals['project_id']).exists()
                    if prj_rec2 and prj_rec2.partner_id:
                        vals['partner_id'] = prj_rec2.partner_id.id

                head_ids = form.getlist('z_head_assignes_ids')
                member_ids = form.getlist('z_member_assignes_ids')
                if head_ids:
                    vals['z_head_assignes_ids'] = [(6, 0, [int(x) for x in head_ids if x])]
                if member_ids:
                    vals['z_member_assignes_ids'] = [(6, 0, [int(x) for x in member_ids if x])]

                tag_ids = form.getlist('tag_ids')
                if tag_ids:
                    vals['tag_ids'] = [(6, 0, [int(x) for x in tag_ids if x])]

                def sf(v):
                    try:
                        return float(v) if v and str(v).strip() else 0.0
                    except Exception:
                        return 0.0

                if form.get('z_bobot_entry') not in (None, ''):
                    try:
                        vals['z_bobot_entry'] = float(form.get('z_bobot_entry'))
                    except:
                        pass

                if form.get('z_progress_project_entry') not in (None, ''):
                    try:
                        vals['z_progress_project_entry'] = float(form.get('z_progress_project_entry'))
                    except:
                        pass
                if form.get('z_quality_entry') not in (None, ''):
                    try:
                        vals['z_quality_entry'] = float(form.get('z_quality_entry'))
                    except:
                        pass

                tech_id = si2(form.get('z_technology_id'))
                sev_id = si2(form.get('z_severity_id'))
                reg_id = si2(form.get('z_regional_id'))
                if tech_id:
                    vals['z_technology_id'] = tech_id
                if sev_id:
                    vals['z_severity_id'] = sev_id
                if reg_id:
                    vals['z_regional_id'] = reg_id

                # Validasi assignee terhadap project team
                proj_for_check = False
                if vals.get('parent_id'):
                    pt = Task.browse(vals['parent_id']).exists()
                    if pt and pt.project_id:
                        proj_for_check = pt.project_id
                if not proj_for_check and vals.get('project_id'):
                    proj_for_check = request.env['project.project'].sudo().browse(vals.get('project_id')).exists()

                if ('z_head_assignes_ids' in vals or 'z_member_assignes_ids' in vals):
                    if proj_for_check:
                        team_ids = set(self._get_project_team_employees(proj_for_check).ids)
                        if 'z_head_assignes_ids' in vals and any(
                                e not in team_ids for e in vals['z_head_assignes_ids'][0][2]):
                            return request.redirect('/portal/tasks?error=Head assignee bukan anggota Project Teams')
                        if 'z_member_assignes_ids' in vals and any(
                                e not in team_ids for e in vals['z_member_assignes_ids'][0][2]):
                            return request.redirect('/portal/tasks?error=Member assignee bukan anggota Project Teams')
                    else:
                        return request.redirect('/portal/tasks?error=Pilih project dulu sebelum assign anggota')

                # PRE-VALIDATION bobot saat create
                if vals.get('z_bobot_entry'):
                    if vals.get('parent_id'):
                        siblings = Task.browse(vals['parent_id']).exists().child_ids
                        base = sum((siblings - siblings.browse([])).mapped('z_bobot_entry'))  # placeholder
                        # Hitung ulang benar:
                        base = sum((siblings).mapped('z_bobot_entry')) if siblings else 0.0
                        total = round(base + vals['z_bobot_entry'], 1)
                    else:
                        pr = request.env['project.project'].sudo().browse(vals.get('project_id')).exists()
                        roots = pr.task_ids.filtered(lambda x: not x.parent_id) if pr else Task.browse([])
                        total = round(sum(roots.mapped('z_bobot_entry')) + vals['z_bobot_entry'], 1)
                    if total > 100:
                        return request.redirect('/portal/tasks?error=Percentage bobot &gt; 100%')

                desc = form.get('description') or ''
                if desc:
                    vals['description'] = desc.replace('\r\n', '\n').replace('\n', '<br/>')

                new_task = Task.create(vals)
                self._finalize_portal_task_after_create(new_task, forced_project_id=intended_project_id)
                return request.redirect(f'/portal/tasks/{new_task.id}?mode=edit&success=Created')
            except Exception as e:
                _logger.exception("Create task error")
                return request.redirect(f"/portal/tasks?error={tools.html_escape(str(e))}")

        # EDIT (GET)
        if task_id and (kw.get('mode') == 'edit' or view_type == 'form'):
            t = Task.browse(task_id).exists()
            if not t:
                return request.redirect('/portal/tasks?error=Task not found')

            root_task = t
            while root_task.parent_id:
                root_task = root_task.parent_id

            employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)

            allow_for_others = (t.z_type_non_project == 'others')

            if request.env.user.has_group('base.group_portal'):
                if (flags['is_engineer'] or flags['is_delivery_support'] or flags['is_readonly_user']):
                    if not allow_for_others:
                        if (not employee) or (employee not in t.z_member_assignes_ids):
                            return request.redirect('/portal/tasks?error=Not allowed')
                if flags['is_head_engineer'] and not (flags['is_pm'] or flags['is_admin']):
                    if not allow_for_others:
                        if (not employee) or (employee not in (t.z_head_assignes_ids | t.z_member_assignes_ids)):
                            return request.redirect('/portal/tasks?error=Not allowed')

            # Timer state
            active_start = ''
            active_running = False
            active_paused = False
            if employee:
                open_line = request.env['account.analytic.line'].sudo().search([
                    ('task_id', '=', t.id),
                    ('employee_id', '=', employee.id),
                    ('z_timesheet_end_date', '=', False)
                ], order='id desc', limit=1)
                if open_line and open_line.z_timesheet_start_date:
                    active_start = fields.Datetime.to_string(open_line.z_timesheet_start_date)
                    active_running = True
                    active_paused = bool(open_line.z_is_paused)

            Req = request.env['account.analytic.line.request'].sudo()
            limit_hist = 20
            pending_reqs = Req.search([
                '|', ('z_task_id', '=', t.id),
                ('z_timesheet_id.task_id', '=', t.id),
                ('z_state', '=', 'waiting_approval')
            ])
            approved_reqs = Req.search([
                '|', ('z_task_id', '=', t.id),
                ('z_timesheet_id.task_id', '=', t.id),
                ('z_state', '=', 'approved')
            ], order='id desc', limit=limit_hist)
            rejected_reqs = Req.search([
                '|', ('z_task_id', '=', t.id),
                ('z_timesheet_id.task_id', '=', t.id),
                ('z_state', '=', 'rejected')
            ], order='id desc', limit=limit_hist)

            pending_reqs = self._filter_requests_by_role(pending_reqs, t, flags, employee)
            approved_reqs = self._filter_requests_by_role(approved_reqs, t, flags, employee)
            rejected_reqs = self._filter_requests_by_role(rejected_reqs, t, flags, employee)

            timesheets = t.timesheet_ids
            if flags['restrict_timesheet_to_self'] and employee:
                timesheets = timesheets.filtered(lambda l: l.employee_id.id == employee.id)

            def _ts_sort_key(rec):
                return rec.z_timesheet_start_date or rec.create_date or fields.Datetime.now()

            timesheets = timesheets.sorted(key=_ts_sort_key, reverse=True)
            ts_page_size = 5
            timesheets_show = timesheets[:ts_page_size]
            ts_pager = {
                'page': 1,
                'total_pages': (len(timesheets) + ts_page_size - 1) // ts_page_size if timesheets else 1,
                'page_size': ts_page_size,
                'total': len(timesheets),
            }

            timesheet_display_map = {}
            for ts in timesheets_show:
                timesheet_display_map[ts.id] = {
                    'start_wib': _to_wib(ts.z_timesheet_start_date),
                    'end_wib': _to_wib(ts.z_timesheet_end_date) if ts.z_timesheet_end_date else False,
                }

            # Update req_disp_map untuk include rejection reason:
            req_disp_map = {}
            for req in (pending_reqs | approved_reqs | rejected_reqs):
                original_start = req.z_timesheet_id.z_timesheet_start_date if req.z_timesheet_id else False
                original_end = req.z_timesheet_id.z_timesheet_end_date if req.z_timesheet_id else False
                req_disp_map[req.id] = {
                    'type': req.z_request_type,
                    'ori_start': _to_wib(original_start),
                    'ori_end': _to_wib(original_end),
                    'new_start': _to_wib(req.z_current_start_date),
                    'new_end': _to_wib(req.z_current_end_date),
                    'hours': round(req.z_current_time_spent, 2),
                    'desc': req.z_name or '',
                    'reject_reason': getattr(req, 'z_reason_reject', '') or '',
                    'reject_reason_desc': getattr(req, 'z_reason_reject_description', '') or '',
                }

            invoice_plans = t.z_invoice_plan_ids.sorted(key=lambda r: (r.z_invoice_date or fields.Date.today(), r.id))
            ip_page_size = 5
            invoice_plans_show = invoice_plans[:ip_page_size]
            ip_pager = {
                'page': 1,
                'total_pages': (len(invoice_plans) + ip_page_size - 1) // ip_page_size if invoice_plans else 1,
                'page_size': ip_page_size,
                'total': len(invoice_plans),
            }

            can_full_edit = not (flags['is_delivery_support'] or flags['is_readonly_user'])
            is_leaf = not bool(t.child_ids)

            portal_task_form_readonly = (
                    (flags.get('is_engineer') or flags.get('is_delivery_support')) and
                    not (flags.get('is_pm') or flags.get('is_head') or flags.get('is_admin'))
            )

            portal_task_form_readonly_all = (
                    flags.get('is_readonly_user') or
                    flags.get('is_engineer') or
                    (flags.get('is_head_engineer') and (t.z_project_task_state in ('approved1', 'approved2', 'done')))
            )

            employees_filtered = self._get_project_team_employees(t.project_id)

            # ===== MASTER TASK FILTER (UPDATED) =====
            Master = request.env['task.master'].sudo()

            def _children_of(mrec):
                return Master.search([('z_parent_id', '=', mrec.id)])

            if not t.parent_id:
                # Root task => tampilkan hanya master root
                master_tasks_filtered = Master.search([('z_parent_id', '=', False)])
            else:
                parent_master = t.parent_id.z_master_task_id
                if parent_master:
                    if not parent_master.z_parent_id:
                        # parent master adalah root → tampilkan anak langsung SAJA (exclude parent kalau ada anak)
                        direct_children = _children_of(parent_master)
                        if direct_children:
                            master_tasks_filtered = direct_children
                        else:
                            master_tasks_filtered = parent_master
                    else:
                        direct_children = _children_of(parent_master)
                        master_tasks_filtered = direct_children if direct_children else parent_master
                else:
                    master_tasks_filtered = Master.search([('z_parent_id', '=', False)])

            if t.z_master_task_id and t.z_master_task_id not in master_tasks_filtered:
                master_tasks_filtered |= t.z_master_task_id

            kw_local = dict(kw)
            if view_type == 'form' and kw_local.get('mode') != 'edit':
                kw_local['mode'] = 'edit'

            nav_prev_id = False
            nav_next_id = False
            nav_index = 0
            nav_total = 0
            try:
                nav_domain = [('project_id', '=', t.project_id.id)]
                # Siblings if has parent, else top-level tasks in project
                nav_domain.append(('parent_id', '=', t.parent_id.id if t.parent_id else False))

                # Apply same restriction as list mode for portal users
                if request.env.user.has_group('base.group_portal'):
                    employee_nav = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)],
                                                                            limit=1)
                    if employee_nav:
                        assignee_or = ['|',
                                       ('z_head_assignes_ids', 'in', [employee_nav.id]),
                                       ('z_member_assignes_ids', 'in', [employee_nav.id])]
                    else:
                        assignee_or = [('id', '=', 0)]
                    must_restrict = (
                            flags['is_engineer'] or
                            flags['is_delivery_support'] or
                            flags['is_readonly_user'] or
                            (flags['is_head'] and not (flags['is_pm'] or flags['is_admin']))
                    )
                    if must_restrict:
                        nav_domain = expression.AND([nav_domain, assignee_or])

                siblings = Task.search(nav_domain, order='id asc')
                sib_ids = [r.id for r in siblings]
                if t.id in sib_ids:
                    idx = sib_ids.index(t.id)
                    nav_index = idx + 1
                    nav_total = len(sib_ids)
                    nav_prev_id = sib_ids[idx - 1] if idx > 0 else False
                    nav_next_id = sib_ids[idx + 1] if idx < nav_total - 1 else False
            except Exception:
                pass

            cur_emp = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
            is_mgr_for_others = False
            if t.z_type_non_project == 'others' and cur_emp:
                # cek hak approve untuk pending requests pada task ini
                for rq in (pending_reqs | approved_reqs | rejected_reqs):
                    emp = rq.z_employee_id or (rq.z_timesheet_id and rq.z_timesheet_id.employee_id)
                    if emp and emp.parent_id and emp.parent_id.id == cur_emp.id:
                        is_mgr_for_others = True
                        break
            t.sudo()._compute_actual_dates()
            t.sudo()._compute_progress()
            t.sudo()._getMandaysBudget()
            t.sudo()._getMandaysBudgetEntry()
            t.sudo()._getActualMandaysBudget()
            t.sudo()._getSubtaskCount()
            t.sudo()._getTimesheetCount()
            t.sudo().onchange_bobot_entry()
            t.sudo()._getProjectTeams()
            t.sudo()._getQualityCalculation()
            t.sudo().action_bobot_sync()
            values = {
                'page_name': 'Edit Task',
                'task': t,
                'task_description': self._convert_html_to_text(t.description or ''),
                'timesheets': timesheets,
                'timesheets_show': timesheets_show,
                'ts_pager': ts_pager,
                'invoice_plans_show': invoice_plans_show,
                'ip_pager': ip_pager,
                'subtasks': request.env['project.task'].sudo().search([('parent_id', '=', t.id)]),

                'pending_requests': pending_reqs,
                'approved_requests': approved_reqs,
                'rejected_requests': rejected_reqs,
                'req_disp_map': req_disp_map,
                'zeiten_map': timesheet_display_map,

                'kw': kw_local,

                # Penting: kirim konteks parent & project dari record task
                'parent_id': t.parent_id.id if t.parent_id else None,
                'parent_task': t.parent_id or False,
                'project_id': t.project_id.id if t.project_id else None,
                'project': t.project_id or False,
                'root_task': root_task,

                'active_timer_start': active_start,
                'active_timer_running': active_running,
                'active_timer_paused': active_paused,

                'nav_prev_id': nav_prev_id,
                'nav_next_id': nav_next_id,
                'nav_index': nav_index,
                'nav_total': nav_total,

                'groupby': '',
                'searchbar_groupings': {},
                'searchbar_sortings': {},

                'can_full_edit': can_full_edit,
                'is_leaf': is_leaf,
                'portal_task_form_readonly': portal_task_form_readonly,
                'portal_task_form_readonly_all': portal_task_form_readonly_all,
                'can_approve_timesheet_for_task': is_mgr_for_others,

                'employees': employees_filtered,
                'master_tasks': master_tasks_filtered,
                'show_back_to_project_link': bool(flags.get('can_view_project_page')),

                **flags,
                **{k: v for k, v in common.items() if k not in ('employees', 'master_tasks')},
            }
            return request.render('z_project.portal_task_page', values)

        # NEW FORM
        if kw.get('mode') == 'new':
            if not flags['can_create_task']:
                return request.redirect('/portal/tasks?error=Not allowed')

            Master = request.env['task.master'].sudo()
            if parent_task:
                pm = parent_task.z_master_task_id
                # NEW: dukung hint master dari URL agar tidak perlu save parent dulu
                parent_master_hint = None
                try:
                    parent_master_hint = int(kw.get('parent_master_hint')) if kw.get('parent_master_hint') else None
                except Exception:
                    parent_master_hint = None
                if not pm and parent_master_hint:
                    pm = Master.browse(parent_master_hint).exists()

                if pm:
                    children = Master.search([('z_parent_id', '=', pm.id)])
                    master_tasks_filtered = children if children else pm
                else:
                    master_tasks_filtered = Master.search([('z_parent_id', '=', False)])
                parent_ctx = {
                    'parent_task': parent_task,
                    'project_id': parent_task.project_id.id if parent_task.project_id else False,
                    'project_name': parent_task.project_id.name if parent_task.project_id else '',
                    'customer_id': parent_task.project_id.partner_id.id if parent_task.project_id and parent_task.project_id.partner_id else False,
                    'customer_name': parent_task.project_id.partner_id.name if parent_task.project_id and parent_task.project_id.partner_id else '',
                    'z_name_of_project': parent_task.project_id.label_tasks or parent_task.project_id.name or '',
                    'z_head_assignes_ids': parent_task.z_head_assignes_ids.ids,
                    'z_member_assignes_ids': parent_task.z_member_assignes_ids.ids,
                    'proj_type': parent_task.project_id.z_type_in_project if parent_task.project_id else '',
                }
            elif project:
                master_tasks_filtered = Master.search([('z_parent_id', '=', False)])
                parent_ctx = {
                    'parent_task': False,
                    'project_id': project.id,
                    'project_name': project.name,
                    'customer_id': project.partner_id.id if project.partner_id else False,
                    'customer_name': project.partner_id.name if project.partner_id else '',
                    'z_name_of_project': project.label_tasks or project.name,
                    'proj_type': project.z_type_in_project or '',
                }
            else:
                master_tasks_filtered = Master.search([('z_parent_id', '=', False)])
                parent_ctx = {'parent_task': False, 'proj_type': ''}

            if parent_task and parent_task.project_id:
                employees_filtered = self._get_project_team_employees(parent_task.project_id)
            elif project:
                employees_filtered = self._get_project_team_employees(project)
            else:
                employees_filtered = request.env['hr.employee'].sudo().browse()

            values = {
                'page_name': 'New Subtask' if parent_id else 'New Task',
                'parent_id': parent_id,
                'project_id': project_id,
                'project': project,
                'task': False,
                'timesheets': False,
                'timesheets_show': False,
                'ts_pager': False,
                'invoice_plans_show': False,
                'ip_pager': False,
                'subtasks': False,
                'active_timer_start': '',
                'active_timer_running': False,
                'active_timer_paused': False,
                'groupby': '',
                'searchbar_groupings': {},
                'searchbar_sortings': {},
                'kw': kw,
                'can_full_edit': True,
                'is_leaf': True,
                'employees': employees_filtered,
                'master_tasks': master_tasks_filtered,
                'show_back_to_project_link': bool(flags.get('can_view_project_page')),
                **parent_ctx,
                **{k: v for k, v in common.items() if k not in ('employees', 'master_tasks')},
                **flags,
            }
            return request.render('z_project.portal_task_page', values)

        # LIST MODE
        domain = []
        if search:
            domain += [
                '|', '|', '|',
                ('name', 'ilike', search),
                ('project_id.name', 'ilike', search),
                ('partner_id.name', 'ilike', search),
                ('z_master_task_id.z_complete_name', 'ilike', search),
            ]
        if parent_id:
            domain.append(('parent_id', '=', parent_id))
        if project_id:
            domain.append(('project_id', '=', project_id))
        if project_id and not parent_id and not task_id and not groupby and not kw.get('include_children'):
            try:
                has_roots = Task.search_count([('project_id', '=', project_id), ('parent_id', '=', False)]) > 0
            except Exception:
                has_roots = True
            if has_roots:
                domain.append(('parent_id', '=', False))

        if task_id and kw.get('mode') not in ('edit', 'new') and view_type != 'form':
            domain.append(('id', '=', task_id))

        if ('groupby' not in request.httprequest.args) and (
                not groupby) and not parent_id and not task_id and not kw.get('mode') and (
                view_type != 'form') and not project_id:
            groupby = 'project_id'

        base_domain = list(domain)

        if request.env.user.has_group('base.group_portal'):
            employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
            if employee:
                assignee_or = ['|',
                               ('z_head_assignes_ids', 'in', [employee.id]),
                               ('z_member_assignes_ids', 'in', [employee.id])]
            else:
                assignee_or = [('id', '=', 0)]
            must_restrict = (
                    flags['is_engineer'] or
                    flags['is_delivery_support'] or
                    flags['is_readonly_user'] or
                    (flags['is_head'] and not (flags['is_pm'] or flags['is_admin']))
            )
            if must_restrict:
                # Izinkan SEMUA task Others terlihat (union), sekaligus tetap restriksi untuk non-Others
                restricted = expression.AND([base_domain, assignee_or]) if employee else expression.AND(
                    [base_domain, [('id', '=', 0)]])
                others_any = expression.AND([base_domain, [('z_type_non_project', '=', 'others')]])
                domain = expression.OR([restricted, others_any])
            else:
                domain = base_domain

        step = 20
        group_step = 50
        pager_header = None
        tasks = []
        common_query = urlencode({
            'search': search, 'sortby': sortby, 'groupby': groupby,
            'parent_id': parent_id or '', 'project_id': project_id or ''
        }, doseq=True)

        depth_map = {}
        running_task_ids = []

        project_root_counts = {}  # project_id -> jumlah root task

        if groupby:
            if groupby not in Task._fields:
                groupby = ''
            else:
                field = Task._fields[groupby]
                read_groups = Task.read_group(domain, fields=[groupby], groupby=[groupby], orderby=groupby, lazy=False)
                data_groups = []
                all_ids_for_running = set()
                for idx, g in enumerate(read_groups):
                    key = g.get(groupby)
                    g_domain = list(domain)
                    label = 'Undefined'
                    gid = False
                    if field.type == 'many2one':
                        gid = key and key[0] or False
                        g_domain.append((groupby, '=', gid))
                        label = key and key[1] or 'Undefined'
                    else:
                        gid = key or False
                        g_domain.append((groupby, '=', gid))
                        if field.type == 'selection':
                            label = dict(field.selection).get(key, 'Undefined')
                        else:
                            label = key or 'Undefined'
                    all_recs = Task.search(g_domain, order=order)
                    all_ids_for_running.update(all_recs.ids)
                    all_recs = self._order_parent_child(all_recs, latest_first=('id desc' in order))
                    dm = self._compute_depth_map(all_recs)
                    depth_map.update(dm)

                    root_count = None
                    if groupby == 'project_id' and gid:
                        try:
                            root_count = Task.search_count([('project_id', '=', gid), ('parent_id', '=', False)])
                            project_root_counts[gid] = root_count
                        except Exception:
                            root_count = None

                    page_param = f'group_page_{idx}'
                    try:
                        page_for_group = int(request.httprequest.args.get(page_param, 1))
                    except Exception:
                        page_for_group = 1
                    if page_for_group < 1:
                        page_for_group = 1
                    offset = (page_for_group - 1) * group_step
                    slice_recs = all_recs[offset: offset + group_step]
                    total_items = len(all_recs)
                    total_pages = (total_items + group_step - 1) // group_step
                    pages = list(range(1, total_pages + 1))
                    data_groups.append({
                        'group_index': idx,
                        'group_value': label,
                        'tasks': slice_recs,
                        'root_count': root_count,
                        'pager': {
                            'page_param': page_param,
                            'page': page_for_group,
                            'step': group_step,
                            'offset': offset,
                            'total': total_items,
                            'pages': pages,
                        }
                    })
                running_task_ids = list(self._get_running_task_ids(all_ids_for_running))
                tasks = data_groups

        if not groupby:
            all_for_order = Task.search(domain, order=order)
            depth_map = self._compute_depth_map(all_for_order)
            running_task_ids = list(self._get_running_task_ids(set(all_for_order.ids)))

            all_for_order = self._order_parent_child(all_for_order, latest_first=('id desc' in order))
            total = len(all_for_order)
            pager_header = pager(
                url='/portal/tasks',
                total=total,
                page=page,
                step=step,
                url_args={'search': search, 'sortby': sortby, 'groupby': groupby,
                          'parent_id': parent_id, 'project_id': project_id}
            )
            tasks = all_for_order[pager_header['offset']: pager_header['offset'] + step]

            if project_id:
                try:
                    project_root_counts[project_id] = Task.search_count(
                        [('project_id', '=', project_id), ('parent_id', '=', False)])
                except Exception:
                    project_root_counts[project_id] = None

        page_name = 'Tasks'
        if parent_task:
            page_name = f'Subtasks of {parent_task.name}'
        elif project:
            page_name = f'Tasks for {project.name}'
        elif task_id and tasks and len(tasks) == 1:
            page_name = f'Task {tasks[0].name}'

        values = {
            'page_name': page_name,
            'tasks': tasks,
            'pager_header': pager_header,
            'search': search,
            'sortby': sortby,
            'groupby': groupby,
            'searchbar_groupings': searchbar_groupings,
            'searchbar_sortings': searchbar_sortings,
            'kw': kw,
            'parent_id': parent_id,
            'parent_task': parent_task,
            'project_id': project_id,
            'project': project,
            'task': False,
            'timesheets': False,
            'timesheets_show': False,
            'ts_pager': False,
            'invoice_plans_show': False,
            'ip_pager': False,
            'subtasks': False,
            'active_timer_start': '',
            'active_timer_running': False,
            'active_timer_paused': False,
            'common_query': common_query,
            'depth_map': depth_map,
            'running_task_ids': running_task_ids,

            'project_root_counts': project_root_counts,

            'can_full_edit': True,
            'is_leaf': False,

            'show_back_to_project_link': bool(flags.get('can_view_project_page')),

            **flags,
            **common,
        }
        return request.render('z_project.portal_task_page', values)

    # ---------------- JSON TIMESHEETS / INVOICE / TIMER / SUBTASK / INVOICE PLAN ROUTES ----------------
    @http.route('/portal/task/<int:task_id>/timesheets/json', type='http', auth='user', methods=['GET'])
    def portal_timesheets_json(self, task_id, page=1, **kw):
        try:
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task not found'}),
                                content_type='application/json')
            flags = self._role_flags()
            employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)

            if request.env.user.has_group('base.group_portal'):
                if (flags['is_engineer'] or flags['is_delivery_support'] or flags['is_readonly_user']) and (
                        not employee or employee not in task.z_member_assignes_ids):
                    return Response(json.dumps({'success': False, 'error': 'Not allowed'}),
                                    content_type='application/json')
                if flags['is_head'] and (not employee or (
                        employee not in task.z_head_assignes_ids and employee not in task.z_member_assignes_ids)):
                    return Response(json.dumps({'success': False, 'error': 'Not allowed'}),
                                    content_type='application/json')

            timesheets = task.timesheet_ids
            if flags['restrict_timesheet_to_self'] and employee:
                timesheets = timesheets.filtered(lambda l: l.employee_id.id == employee.id)

            def _ts_sort_key(rec):
                return rec.z_timesheet_start_date or rec.create_date or fields.Datetime.now()

            timesheets = timesheets.sorted(key=_ts_sort_key, reverse=True)

            try:
                page = int(page)
            except:
                page = 1
            if page < 1:
                page = 1
            page_size = 5
            total = len(timesheets)
            total_pages = (total + page_size - 1) // page_size if total else 1
            if page > total_pages:
                page = total_pages
            offset = (page - 1) * page_size
            subset = timesheets[offset: offset + page_size]

            def _fmt_duration(hours_float):
                total_minutes = int(round((hours_float or 0) * 60))
                h = total_minutes // 60
                m = total_minutes % 60
                return f"{h:02}:{m:02}"

            row_html_list = []
            card_list = []
            for ts in subset:
                start_raw = ts.z_timesheet_start_date and fields.Datetime.to_string(ts.z_timesheet_start_date) or ''
                end_raw = ts.z_timesheet_end_date and fields.Datetime.to_string(ts.z_timesheet_end_date) or ''
                dur_text = _fmt_duration(ts.unit_amount or 0.0)
                dur_html = f"{dur_text}<small class='text-muted d-block'>({round(ts.unit_amount or 0, 2)}h)</small>"

                if ts.z_state == 'approved':
                    state_html = "<span class='badge bg-success'>Approved</span>"
                elif ts.z_state == 'waiting_approval':
                    state_html = "<span class='badge bg-warning text-dark'>Waiting</span>"
                elif ts.z_state == 'draft':
                    state_html = "<span class='badge bg-secondary'>Draft</span>"
                else:
                    state_html = f"<span class='badge bg-light text-dark'>{tools.html_escape(ts.z_state or 'Undefined')}</span>"

                btns = ""
                # Tampilkan Edit/Delete hanya jika status task belum approved1..done
                if task.z_project_task_state not in ('approved1', 'approved2', 'done'):
                    btns = f"""
                        <button type="button" class="btn btn-sm btn-outline-primary edit-timesheet-btn"
                                data-task-id="{task.id}" data-timesheet-id="{ts.id}">
                            <i class="fa fa-edit"></i>
                        </button>
                    """
                    if flags['can_delete_timesheet']:
                        btns += f"""
                            <form action="/portal/task/{task.id}/timesheet/delete/{ts.id}" method="post" class="d-inline-block ms-1">
                                <input type="hidden" name="csrf_token" value="{request.csrf_token()}"/>
                                <button type="submit" class="btn btn-sm btn-outline-danger" onclick="return confirm('Delete?')">
                                    <i class="fa fa-trash"></i>
                                </button>
                            </form>
                    """

                row_html_list.append(f"""
                    <tr>
                      <td><span class="timesheet-datetime" data-origin="utc" data-utc="{start_raw}">{start_raw}</span></td>
                      <td>{(f'<span class="timesheet-datetime" data-origin="utc" data-utc="{end_raw}">{end_raw}</span>' if end_raw else "<span class='badge bg-success'>RUNNING</span>")}</td>
                      <td>{tools.html_escape(ts.employee_id.name or '')}</td>
                      <td>{tools.html_escape(ts.name or '')}</td>
                      <td>{dur_html}</td>
                      <td>{state_html}</td>
                      <td>{btns}</td>
                    </tr>
                """)

                card_btns = f"""
                    <button type="button" class="btn btn-sm btn-outline-primary edit-timesheet-btn"
                            data-task-id="{task.id}" data-timesheet-id="{ts.id}">
                        <i class="fa fa-edit"></i>
                    </button>
                """
                if flags['can_delete_timesheet']:
                    card_btns += f"""
                        <form action="/portal/task/{task.id}/timesheet/delete/{ts.id}" method="post" class="d-inline-block">
                            <input type="hidden" name="csrf_token" value="{request.csrf_token()}"/>
                            <button type="submit" class="btn btn-sm btn-outline-danger"
                                    onclick="return confirm('Delete?')">
                              <i class="fa fa-trash"></i>
                            </button>
                        </form>
                    """
                card_list.append(f"""
                    <div class="col-12">
                      <div class="card shadow-sm">
                        <div class="card-body p-3">
                          <div class="d-flex justify-content-between mb-1">
                            <strong>{tools.html_escape(ts.employee_id.name or '')}</strong>
                            {state_html}
                          </div>
                          <div class="small">
                            <strong>Start:</strong> <span class="timesheet-datetime" data-origin="utc" data-utc="{start_raw}">{start_raw}</span><br/>
                            <strong>End:</strong> {(f'<span class="timesheet-datetime" data-origin="utc" data-utc="{end_raw}">{end_raw}</span>' if end_raw else 'Running...')}
                          </div>
                          <div class="mt-1 small"><strong>Duration:</strong>
                            <span class="badge bg-primary ms-1">{dur_text}</span>
                          </div>
                          <div class="mt-1 small"><strong>Desc:</strong> {tools.html_escape(ts.name or '')}</div>
                          <div class="mt-2 d-flex gap-2">{card_btns}</div>
                        </div>
                      </div>
                    </div>
                """)

            has_more = page < total_pages
            return Response(json.dumps({
                'success': True,
                'page': page,
                'total_pages': total_pages,
                'has_more': has_more,
                'items_html': "".join(row_html_list),
                'cards_html': "".join(card_list),
            }), content_type='application/json')
        except Exception as e:
            _logger.error("Timesheets JSON error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- JSON INVOICE PLANS ----------------
    @http.route('/portal/task/<int:task_id>/invoice-plans/json', type='http', auth='user', methods=['GET'])
    def portal_invoice_plans_json(self, task_id, page=1, **kw):
        try:
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task not found'}),
                                content_type='application/json')
            flags = self._role_flags()
            if not flags['show_tab_invoice_plan']:
                return Response(json.dumps({'success': False, 'error': 'Not allowed'}), content_type='application/json')

            invoice_plans = task.z_invoice_plan_ids.sorted(
                key=lambda r: (r.z_invoice_date or fields.Date.today(), r.id)
            )
            try:
                page = int(page)
            except:
                page = 1
            if page < 1:
                page = 1
            page_size = 5
            total = len(invoice_plans)
            total_pages = (total + page_size - 1) // page_size if total else 1
            if page > total_pages:
                page = total_pages
            offset = (page - 1) * page_size
            subset = invoice_plans[offset: offset + page_size]

            rows = []
            cards = []
            for inv in subset:
                date_txt = inv.z_invoice_date.strftime('%d/%m/%Y') if inv.z_invoice_date else ''
                state_class = 'bg-secondary'
                if inv.z_state == 'paid':
                    state_class = 'bg-success'
                elif inv.z_state == 'sent':
                    state_class = 'bg-warning'
                row_action = ""
                card_action = ""
                if flags['can_edit_invoice_plan']:
                    row_action = f"""
                        <button type="button" class="btn btn-sm btn-outline-primary edit-invoice-plan-btn"
                                data-task-id="{task.id}" data-invoice-id="{inv.id}">
                            <i class="fa fa-edit"></i>
                        </button>
                        <form action="/portal/task/{task.id}/invoice-plan/delete/{inv.id}" method="post"
                              class="d-inline-block ms-1">
                            <input type="hidden" name="csrf_token" value="{request.csrf_token()}"/>
                            <button type="submit" class="btn btn-sm btn-outline-danger"
                                    onclick="return confirm('Delete invoice plan?')">
                                <i class="fa fa-trash"></i>
                            </button>
                        </form>
                        """
                    card_action = f"""
                        <div class="mt-2 d-flex gap-2">
                            <button type="button" class="btn btn-sm btn-outline-primary edit-invoice-plan-btn"
                                    data-task-id="{task.id}" data-invoice-id="{inv.id}">
                                <i class="fa fa-edit"></i> Edit
                            </button>
                            <form action="/portal/task/{task.id}/invoice-plan/delete/{inv.id}" method="post"
                                  class="d-inline-block">
                                <input type="hidden" name="csrf_token" value="{request.csrf_token()}"/>
                                <button type="submit" class="btn btn-sm btn-outline-danger"
                                        onclick="return confirm('Delete invoice plan?')">
                                    <i class="fa fa-trash"></i> Del
                                </button>
                            </form>
                        </div>
                        """
                rows.append(f"""
                    <tr>
                      <td>{tools.html_escape(inv.z_number_of_invoice or '')}</td>
                      <td>{tools.html_escape(inv.z_name or '')}</td>
                      <td>{date_txt}</td>
                      <td>Rp {'{:,.2f}'.format(inv.z_amount_total)}</td>
                      <td><span class="badge {state_class}">{tools.html_escape((inv.z_state or '').title())}</span></td>
                      <td>{row_action}</td>
                    </tr>
                    """)
                cards.append(f"""
                    <div class="col-12">
                      <div class="card shadow-sm">
                        <div class="card-body p-3">
                          <div class="d-flex justify-content-between align-items-start mb-2">
                            <h6 class="mb-0">{tools.html_escape(inv.z_number_of_invoice or '')}</h6>
                            <span class="badge {state_class}">{tools.html_escape((inv.z_state or '').title())}</span>
                          </div>
                          <p class="text-muted mb-2">{tools.html_escape(inv.z_name or '')}</p>
                          <div class="row small mb-2">
                            <div class="col-6"><strong>Date:</strong> {date_txt}</div>
                            <div class="col-6"><strong>Amount:</strong> Rp {'{:,.2f}'.format(inv.z_amount_total)}</div>
                          </div>
                          {card_action}
                        </div>
                      </div>
                    </div>
                    """)

            has_more = page < total_pages
            resp = {
                'success': True,
                'page': page,
                'total_pages': total_pages,
                'has_more': has_more,
                'items_html': "".join(rows),
                'cards_html': "".join(cards),
            }
            return Response(json.dumps(resp), content_type='application/json')
        except Exception as e:
            _logger.error("Invoice plan JSON error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- TIMER / TIMESHEET APIs ----------------
    @http.route('/portal/task/<int:task_id>/timer', type='http', auth='user', website=True, methods=['POST'])
    def portal_task_timer(self, task_id, **post):
        try:
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task not found'}),
                                content_type='application/json')
            employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
            if not employee:
                return Response(json.dumps({'success': False, 'error': 'Employee not found'}),
                                content_type='application/json')
            action = post.get('action')
            AAL = request.env['account.analytic.line'].sudo()

            if action == 'start' and not task.z_member_assignes_ids:
                return Response(json.dumps({'success': False, 'error': 'Tidak bisa start: belum ada member assignee'}),
                                content_type='application/json')

            def _open():
                return AAL.search([
                    ('task_id', '=', task.id),
                    ('employee_id', '=', employee.id),
                    ('z_timesheet_end_date', '=', False)
                ], order='id desc', limit=1)

            if action == 'start':
                line = _open()
                now = fields.Datetime.now()
                if line:
                    # Pastikan status minimal in_progress ketika timer berjalan
                    if task.z_project_task_state == 'new':
                        task.write({'z_project_task_state': 'in_progress'})
                    return Response(json.dumps(
                        {'success': True, 'start_at': fields.Datetime.to_string(line.z_timesheet_start_date),
                         'action': 'already_running'}), content_type='application/json')

                line = AAL.create({
                    'task_id': task.id,
                    'project_id': task.project_id.id if task.project_id else False,
                    'employee_id': employee.id,
                    'name': f'Timer started for {task.name}',
                    'z_timesheet_start_date': now,
                    'date': now.date(),
                    'z_is_paused': False,
                    'z_pause_started_at': False,
                    'z_pause_accum_seconds': 0,
                    'z_timer_state': 'running',
                    'z_state': 'draft',
                })
                # Ubah status ke in_progress saat timer start
                if task.z_project_task_state == 'new':
                    task.write({'z_project_task_state': 'in_progress'})
                return Response(json.dumps(
                    {'success': True, 'start_at': fields.Datetime.to_string(now), 'action': 'started',
                     'timesheet_id': line.id}), content_type='application/json')

            elif action == 'pause':
                line = _open()
                if not line:
                    return Response(json.dumps({'success': False, 'error': 'No running timer'}),
                                    content_type='application/json')
                if line.z_is_paused:
                    return Response(json.dumps({'success': True, 'action': 'paused'}), content_type='application/json')
                if hasattr(line, 'action_pause_timer'):
                    line.action_pause_timer()
                else:
                    line.write({'z_is_paused': True, 'z_pause_started_at': fields.Datetime.now()})
                return Response(json.dumps({'success': True, 'action': 'paused'}), content_type='application/json')

            elif action == 'resume':
                line = _open()
                if not line:
                    return Response(json.dumps({'success': False, 'error': 'No paused timer'}),
                                    content_type='application/json')
                if not line.z_is_paused:
                    return Response(json.dumps({'success': True, 'action': 'running',
                                                'start_at': fields.Datetime.to_string(line.z_timesheet_start_date)}),
                                    content_type='application/json')
                if hasattr(line, 'action_resume_timer'):
                    line.action_resume_timer()
                else:
                    pause_started = line.z_pause_started_at
                    accum = line.z_pause_accum_seconds or 0
                    if pause_started:
                        delta = (fields.Datetime.now() - pause_started).total_seconds()
                        accum += max(0, int(delta))
                    line.write({
                        'z_is_paused': False,
                        'z_pause_started_at': False,
                        'z_pause_accum_seconds': accum
                    })
                return Response(json.dumps({'success': True, 'action': 'resumed',
                                            'start_at': fields.Datetime.to_string(line.z_timesheet_start_date)}),
                                content_type='application/json')


            elif action == 'stop':

                desc = (post.get('description') or '').strip()

                if not desc:
                    return Response(json.dumps({'success': False, 'error': 'Description required'}),

                                    content_type='application/json')

                line = _open()

                if not line:
                    return Response(json.dumps({'success': False, 'error': 'No running timer'}),

                                    content_type='application/json')

                if line.z_is_paused and hasattr(line, 'action_resume_timer'):
                    line.action_resume_timer()

                end_time = fields.Datetime.now()

                # TIDAK perlu hitung manual; compute unit_amount akan terpicu oleh write start/end

                line.write({

                    'z_timesheet_end_date': end_time,

                    'name': desc,

                    'z_timer_state': 'stopped',

                    'z_state': 'approved',

                })

                attachment_ids = []

                if hasattr(request, 'httprequest') and request.httprequest.files:

                    for f in request.httprequest.files.getlist('attachments'):

                        if f and f.filename:

                            try:

                                content = f.read()

                                att = request.env['ir.attachment'].sudo().create({

                                    'name': f.filename,

                                    'type': 'binary',

                                    'datas': base64.b64encode(content),

                                    'res_model': 'account.analytic.line',

                                    'res_id': line.id,

                                    'mimetype': f.content_type or 'application/octet-stream',

                                    'public': False,

                                })

                                attachment_ids.append(att.id)

                                self._create_document_from_attachment(att, task)

                            except Exception as ex:

                                _logger.error("Attachment upload error: %s", ex)

                if attachment_ids:
                    line.message_post(body=f"Timer stopped with {len(attachment_ids)} attachment(s).",

                                      attachment_ids=attachment_ids)

                return Response(json.dumps({'success': True, 'action': 'stopped', 'timesheet_id': line.id}),

                                content_type='application/json')

            return Response(json.dumps({'success': False, 'error': 'Invalid action'}), content_type='application/json')
        except Exception as e:
            _logger.error("Timer error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- TIMESHEET MANUAL ----------------
    @http.route('/portal/task/<int:task_id>/timesheet', type='http', auth='user', website=True, methods=['POST'])
    def portal_save_timesheet(self, task_id, **kw):
        try:
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'error': 'Task not found'}), content_type='application/json')
            Line = request.env['account.analytic.line'].sudo()
            timesheet_id = int(kw.get('timesheet_id') or 0)
            desc = (kw.get('description') or '').strip()
            employee_id = int(kw.get('employee_id') or 0) if kw.get('employee_id') else False
            start_dt = _parse_datetime_to_utc(kw.get('start_date'))
            end_dt = _parse_datetime_to_utc(kw.get('end_date'))
            if not start_dt or not end_dt:
                return Response(json.dumps({'error': 'Start and End required'}), content_type='application/json')
            if end_dt <= start_dt:
                return Response(json.dumps({'error': 'End must be greater than Start'}),
                                content_type='application/json')
            vals = {
                'task_id': task.id,
                'project_id': task.project_id.id if task.project_id else False,
                'employee_id': employee_id,
                'name': desc or 'Manual Timesheet',
                'z_timesheet_start_date': start_dt,
                'z_timesheet_end_date': end_dt,
                # unit_amount dihitung otomatis oleh compute (berdasarkan working schedule)
                'date': start_dt.date(),
                'z_state': 'waiting_approval',
            }
            if timesheet_id:
                line = Line.browse(timesheet_id).exists()
                if not line or line.task_id.id != task.id:
                    return Response(json.dumps({'error': 'Timesheet not found'}), content_type='application/json')
                line.write(vals)
            else:
                Line.create(vals)
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            _logger.error("Manual timesheet error: %s", e)
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    # ---------------- TIMESHEET GET ----------------
    @http.route('/portal/task/<int:task_id>/timesheet/<int:timesheet_id>', type='http', auth='user', website=True)
    def portal_get_timesheet(self, task_id, timesheet_id):
        try:
            ts = request.env['account.analytic.line'].sudo().browse(timesheet_id).exists()
            if not ts or ts.task_id.id != task_id:
                return Response(json.dumps({'error': 'Timesheet not found'}), content_type='application/json')
            pending_correction = bool(request.env['account.analytic.line.request'].sudo().search([
                ('z_timesheet_id', '=', ts.id),
                ('z_state', '=', 'waiting_approval'),
                ('z_request_type', '=', 'correction')
            ], limit=1))
            data = {
                'id': ts.id,
                'description': ts.name or '',
                'start_date': _format_datetime_to_user_tz(ts.z_timesheet_start_date),
                'end_date': _format_datetime_to_user_tz(ts.z_timesheet_end_date),
                'hours': ts.unit_amount,
                'employee_id': ts.employee_id.id if ts.employee_id else False,
                'employee_name': ts.employee_id.name if ts.employee_id else '',
                'original_start_date': _format_datetime_to_user_tz(ts.z_timesheet_start_date),
                'original_end_date': _format_datetime_to_user_tz(ts.z_timesheet_end_date),
                'pending': pending_correction,
            }
            return Response(json.dumps(data), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    # ---------------- TIMESHEET DELETE ----------------
    @http.route('/portal/task/<int:task_id>/timesheet/delete/<int:timesheet_id>', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_delete_timesheet(self, task_id, timesheet_id, **kw):
        try:
            flags = self._role_flags()
            if not flags['can_delete_timesheet']:
                return Response(json.dumps({'error': 'Not allowed'}), content_type='application/json')
            ts = request.env['account.analytic.line'].sudo().browse(timesheet_id).exists()
            if ts and ts.task_id.id == task_id:
                ts.unlink()
                return Response(json.dumps({'success': True}), content_type='application/json')
            return Response(json.dumps({'error': 'Timesheet not found'}), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    # ---------------- TIMESHEET REQUEST (NEW) ----------------
    @http.route('/portal/task/<int:task_id>/timesheet-request', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_timesheet_request(self, task_id, **kw):
        try:
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task not found'}),
                                content_type='application/json')
            employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
            if not employee:
                return Response(json.dumps({'success': False, 'error': 'Employee not found'}),
                                content_type='application/json')
            if task.z_project_task_state in ('approved1', 'approved2', 'done'):
                return Response(json.dumps({'success': False, 'error': 'Not allowed at this stage'}),
                                content_type='application/json')

            start_dt = _parse_datetime_to_utc(kw.get('start_date'))
            end_dt = _parse_datetime_to_utc(kw.get('end_date'))
            desc = (kw.get('description') or '').strip()
            if not start_dt or not end_dt:
                return Response(json.dumps({'success': False, 'error': 'Start and End required'}),
                                content_type='application/json')
            if end_dt <= start_dt:
                return Response(json.dumps({'success': False, 'error': 'End must be greater than Start'}),
                                content_type='application/json')
            Req = request.env['account.analytic.line.request'].sudo()
            req = Req.create({
                'z_request_type': 'new',
                'z_task_id': task.id,
                'z_employee_id': employee.id,
                'z_name': desc or f'Request Timesheet {task.name}',
                'z_current_start_date': start_dt,
                'z_current_end_date': end_dt,
                'z_state': 'waiting_approval',
            })
            if task.z_project_task_state == 'new':
                task.write({'z_project_task_state': 'in_progress'})

            return Response(json.dumps({'success': True, 'request_id': req.id}), content_type='application/json')
        except Exception as e:
            _logger.error("Timesheet request error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- TIMESHEET CORRECTION ----------------
    @http.route('/portal/task/<int:task_id>/timesheet-correction', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_timesheet_correction(self, task_id, **kw):
        try:
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task not found'}),
                                content_type='application/json')
            AAL = request.env['account.analytic.line'].sudo()
            Req = request.env['account.analytic.line.request'].sudo()
            ts_id = int(kw.get('timesheet_id') or 0)
            ts = AAL.browse(ts_id).exists()
            if not ts or ts.task_id.id != task_id:
                return Response(json.dumps({'success': False, 'error': 'Timesheet not found'}),
                                content_type='application/json')
            employee = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)
            if not employee:
                return Response(json.dumps({'success': False, 'error': 'Employee not found'}),
                                content_type='application/json')
            # Dilarang koreksi jika status sudah approved1..done
            if task.z_project_task_state in ('approved1', 'approved2', 'done'):
                return Response(json.dumps({'success': False, 'error': 'Not allowed at this stage'}),
                                content_type='application/json')
            start_dt = _parse_datetime_to_utc(kw.get('start_date'))
            end_dt = _parse_datetime_to_utc(kw.get('end_date'))
            desc = (kw.get('description') or '').strip()
            if not start_dt or not end_dt:
                return Response(json.dumps({'success': False, 'error': 'Start & End required'}),
                                content_type='application/json')
            if end_dt <= start_dt:
                return Response(json.dumps({'success': False, 'error': 'End must be greater than Start'}),
                                content_type='application/json')
            same_start = ts.z_timesheet_start_date and ts.z_timesheet_start_date.replace(second=0,
                                                                                         microsecond=0) == start_dt
            same_end = ts.z_timesheet_end_date and ts.z_timesheet_end_date.replace(second=0, microsecond=0) == end_dt
            same_desc = (ts.name or '').strip() == desc
            if same_start and same_end and same_desc:
                return Response(json.dumps({'success': False, 'error': 'No changes'}), content_type='application/json')
            pending = Req.search([
                ('z_timesheet_id', '=', ts.id),
                ('z_state', '=', 'waiting_approval'),
                ('z_request_type', '=', 'correction')
            ], limit=1)
            if pending:
                return Response(json.dumps({'success': False, 'error': 'Pending request exists'}),
                                content_type='application/json')
            req = Req.create({
                'z_request_type': 'correction',
                'z_timesheet_id': ts.id,
                'z_task_id': task.id,
                'z_employee_id': employee.id,
                'z_name': desc or ts.name,
                'z_current_start_date': start_dt,
                'z_current_end_date': end_dt,
                # z_current_time_spent dihitung otomatis oleh compute (berdasarkan working schedule)
                'z_ori_start_date': ts.z_timesheet_start_date,
                'z_ori_end_date': ts.z_timesheet_end_date,
                'z_state': 'waiting_approval',
            })
            # JANGAN ubah status timesheet asli saat correction diajukan
            return Response(json.dumps({'success': True, 'request_id': req.id}), content_type='application/json')
        except Exception as e:
            _logger.error("Timesheet correction error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    @http.route('/portal/timesheet-correction/<int:req_id>/approve', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_approve_timesheet_correction(self, req_id, **kw):
        try:
            Req = request.env['account.analytic.line.request'].sudo().browse(req_id).exists()
            if not Req or Req.z_request_type != 'correction':
                return Response(json.dumps({'success': False, 'error': 'Request not found'}),
                                content_type='application/json')

            flags = self._role_flags()
            is_pm = flags.get('is_pm')
            is_admin = flags.get('is_admin')

            task = Req.z_task_id or (Req.z_timesheet_id and Req.z_timesheet_id.task_id)
            req_emp = Req.z_employee_id
            cur_emp = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)

            allowed = False
            if task and task.z_type_non_project == 'others':
                is_manager = bool(cur_emp and req_emp and req_emp.parent_id and req_emp.parent_id.id == cur_emp.id)
                allowed = bool(is_pm or is_admin or is_manager)
            else:
                allowed = bool(flags.get('can_approve_timesheet'))

            if not allowed:
                return Response(json.dumps({'success': False, 'error': 'Not allowed'}), content_type='application/json')

            Req.action_approve()
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            _logger.error("Approve correction error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    @http.route('/portal/timesheet-correction/<int:req_id>/reject', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_reject_timesheet_correction(self, req_id, **kw):
        try:
            Req = request.env['account.analytic.line.request'].sudo().browse(req_id).exists()
            if not Req or Req.z_request_type != 'correction':
                return Response(json.dumps({'success': False, 'error': 'Request not found'}),
                                content_type='application/json')

            flags = self._role_flags()
            is_pm = flags.get('is_pm')
            is_admin = flags.get('is_admin')

            task = Req.z_task_id or (Req.z_timesheet_id and Req.z_timesheet_id.task_id)
            req_emp = Req.z_employee_id
            cur_emp = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)

            allowed = False
            if task and task.z_type_non_project == 'others':
                is_manager = bool(cur_emp and req_emp and req_emp.parent_id and req_emp.parent_id.id == cur_emp.id)
                allowed = bool(is_pm or is_admin or is_manager)
            else:
                allowed = bool(flags.get('can_approve_timesheet'))

            if not allowed:
                return Response(json.dumps({'success': False, 'error': 'Not allowed'}), content_type='application/json')

            reason = kw.get('reason', '').strip()
            if hasattr(Req, 'action_reject'):
                Req.with_context(rejection_reason=reason).action_reject()
            else:
                vals = {'z_state': 'rejected'}
                if reason and hasattr(Req, 'z_reason_reject'):
                    vals['z_reason_reject'] = reason
                if reason and hasattr(Req, 'z_reason_reject_description'):
                    vals['z_reason_reject_description'] = reason
                Req.write(vals)
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            _logger.error("Reject correction error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- BACKDATE (NEW ENTRY) APPROVE / REJECT ----------------
    @http.route('/portal/timesheet-request/<int:req_id>/approve', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_approve_timesheet_request(self, req_id, **kw):
        try:
            Req = request.env['account.analytic.line.request'].sudo().browse(req_id).exists()
            if not Req or Req.z_request_type != 'new':
                return Response(json.dumps({'success': False, 'error': 'Request not found'}),
                                content_type='application/json')

            flags = self._role_flags()
            is_pm = flags.get('is_pm')
            is_admin = flags.get('is_admin')

            task = Req.z_task_id
            req_emp = Req.z_employee_id
            cur_emp = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)

            allowed = False
            if task and task.z_type_non_project == 'others':
                is_manager = bool(cur_emp and req_emp and req_emp.parent_id and req_emp.parent_id.id == cur_emp.id)
                allowed = bool(is_pm or is_admin or is_manager)
            else:
                allowed = bool(flags.get('can_approve_timesheet'))

            if not allowed:
                return Response(json.dumps({'success': False, 'error': 'Not allowed'}), content_type='application/json')

            Req.action_approve()
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            _logger.error("Approve backdate error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    @http.route('/portal/timesheet-request/<int:req_id>/reject', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_reject_timesheet_request(self, req_id, **kw):
        try:
            Req = request.env['account.analytic.line.request'].sudo().browse(req_id).exists()
            if not Req or Req.z_request_type != 'new':
                return Response(json.dumps({'success': False, 'error': 'Request not found'}),
                                content_type='application/json')

            flags = self._role_flags()
            is_pm = flags.get('is_pm')
            is_admin = flags.get('is_admin')

            task = Req.z_task_id
            req_emp = Req.z_employee_id
            cur_emp = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], limit=1)

            allowed = False
            if task and task.z_type_non_project == 'others':
                is_manager = bool(cur_emp and req_emp and req_emp.parent_id and req_emp.parent_id.id == cur_emp.id)
                allowed = bool(is_pm or is_admin or is_manager)
            else:
                allowed = bool(flags.get('can_approve_timesheet'))

            if not allowed:
                return Response(json.dumps({'success': False, 'error': 'Not allowed'}), content_type='application/json')

            reason = kw.get('reason', '').strip()
            if hasattr(Req, 'action_reject'):
                Req.with_context(rejection_reason=reason).action_reject()
            else:
                vals = {'z_state': 'rejected'}
                if reason and hasattr(Req, 'z_reason_reject'):
                    vals['z_reason_reject'] = reason
                if reason and hasattr(Req, 'z_reason_reject_description'):
                    vals['z_reason_reject_description'] = reason
                Req.write(vals)
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            _logger.error("Reject request error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    # ---------------- SUBTASK CRUD ----------------
    @http.route('/portal/task/<int:task_id>/subtask', type='http', auth='user', website=True, methods=['POST'])
    def portal_save_subtask(self, task_id, **post):
        try:
            flags = self._role_flags()
            if not flags['can_create_subtask']:
                return Response(json.dumps({'success': False, 'error': 'Not allowed'}), content_type='application/json')
            parent_task = request.env['project.task'].sudo().browse(task_id).exists()
            if not parent_task:
                return Response(json.dumps({'success': False, 'error': 'Task not found'}),
                                content_type='application/json')

            subtask_id = int(post.get('subtask_id') or 0)
            head_ids = request.httprequest.form.getlist('z_head_assignes_ids')
            member_ids = request.httprequest.form.getlist('z_member_assignes_ids')

            vals = {
                'z_master_task_id': int(post.get('z_master_task_id')) if post.get('z_master_task_id') else False,
                'z_project_task_state': post.get('z_project_task_state', 'new'),
                'z_head_assignes_ids': [(6, 0, [int(x) for x in head_ids if x])],
                'z_member_assignes_ids': [(6, 0, [int(x) for x in member_ids if x])],
                'parent_id': parent_task.id,
                'project_id': parent_task.project_id.id if parent_task.project_id else False,
                'partner_id': parent_task.partner_id.id if parent_task.partner_id else False,
            }
            TaskModel = request.env['project.task'].sudo()
            if subtask_id:
                st = TaskModel.browse(subtask_id).exists()
                if not st:
                    return Response(json.dumps({'success': False, 'error': 'Subtask not found'}),
                                    content_type='application/json')
                st.write(vals)
                if not re.fullmatch(rf'{re.escape(parent_task.name)}\.\d{{2}}', st.name or ''):
                    st.generate_sequence_name()
            else:
                st = TaskModel.create(vals)
                if not re.fullmatch(rf'{re.escape(parent_task.name)}\.\d{{2}}', st.name or ''):
                    st.generate_sequence_name()

            return Response(json.dumps({'success': True, 'id': st.id}), content_type='application/json')
        except Exception as e:
            _logger.error("Save subtask error: %s", e)
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')

    @http.route('/portal/task/<int:task_id>/subtask/<int:subtask_id>', type='http', auth='user', website=True)
    def portal_get_subtask(self, task_id, subtask_id, **kw):
        try:
            st = request.env['project.task'].sudo().browse(subtask_id).exists()
            if not st or st.parent_id.id != task_id:
                return Response(json.dumps({'error': 'Subtask not found'}), content_type='application/json')
            data = {
                'id': st.id,
                'name': st.name,
                'z_master_task_id': st.z_master_task_id.id if st.z_master_task_id else False,
                'z_project_task_state': st.z_project_task_state,
                'z_head_assignes_ids': st.z_head_assignes_ids.ids,
                'z_member_assignes_ids': st.z_member_assignes_ids.ids,
            }
            return Response(json.dumps(data), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    @http.route('/portal/task/<int:task_id>/subtask/delete/<int:subtask_id>', type='http', auth='user', website=True,
                methods=['POST'])
    def portal_delete_subtask(self, task_id, subtask_id, **kw):
        try:
            flags = self._role_flags()
            if not flags['can_delete_subtask']:
                return Response(json.dumps({'error': 'Not allowed'}), content_type='application/json')
            st = request.env['project.task'].sudo().browse(subtask_id).exists()
            if st and st.parent_id.id == task_id:
                st.unlink()
                return Response(json.dumps({'success': True}), content_type='application/json')
            return Response(json.dumps({'error': 'Subtask not found'}), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    # ---------------- INVOICE PLAN CRUD ----------------
    @http.route('/portal/task/<int:task_id>/invoice-plan', type='http', auth='user', website=True, methods=['POST'])
    def portal_save_invoice_plan(self, task_id, **kw):
        try:
            flags = self._role_flags()
            if not flags['can_edit_invoice_plan']:
                return Response(json.dumps({'error': 'Not allowed'}), content_type='application/json')
            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'error': 'Task not found'}), content_type='application/json')
            ip_id = int(kw.get('invoice_plan_id') or 0)

            def sf(v):
                try:
                    return float(v) if v and str(v).strip() else 0.0
                except Exception:
                    return 0.0

            vals = {
                'z_invoce_plan_id': task.id,
                'z_name': kw.get('z_name', ''),
                'z_number_of_invoice': kw.get('z_number_of_invoice', ''),
                'z_invoice_date': kw.get('z_invoice_date') or False,
                'z_amount_total': sf(kw.get('z_amount_total')),
                'z_state': kw.get('z_state', 'draft'),
            }
            Model = request.env['project.task.invoice.plan'].sudo()
            if ip_id:
                ip = Model.browse(ip_id).exists()
                if not ip or ip.z_invoce_plan_id.id != task.id:
                    return Response(json.dumps({'error': 'Invoice plan not found'}), content_type='application/json')
                ip.write(vals)
            else:
                Model.create(vals)
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            _logger.error("Invoice plan save error: %s", e)
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    @http.route('/portal/task/<int:task_id>/invoice-plan/<int:invoice_plan_id>', type='http', auth='user', website=True)
    def portal_get_invoice_plan(self, task_id, invoice_plan_id):
        try:
            ip = request.env['project.task.invoice.plan'].sudo().browse(invoice_plan_id).exists()
            if not ip or ip.z_invoce_plan_id.id != task_id:
                return Response(json.dumps({'error': 'Invoice plan not found'}), content_type='application/json')
            data = {
                'id': ip.id,
                'z_name': ip.z_name or '',
                'z_number_of_invoice': ip.z_number_of_invoice or '',
                'z_invoice_date': ip.z_invoice_date.strftime('%Y-%m-%d') if ip.z_invoice_date else '',
                'z_amount_total': ip.z_amount_total,
                'z_state': ip.z_state or 'draft'
            }
            return Response(json.dumps(data), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    @http.route('/portal/task/<int:task_id>/invoice-plan/delete/<int:invoice_plan_id>', type='http', auth='user',
                website=True, methods=['POST'])
    def portal_delete_invoice_plan(self, task_id, invoice_plan_id, **kw):
        try:
            flags = self._role_flags()
            if not flags['can_edit_invoice_plan']:
                return Response(json.dumps({'error': 'Not allowed'}), content_type='application/json')
            ip = request.env['project.task.invoice.plan'].sudo().browse(invoice_plan_id).exists()
            if ip and ip.z_invoce_plan_id.id == task_id:
                ip.unlink()
                return Response(json.dumps({'success': True}), content_type='application/json')
            return Response(json.dumps({'error': 'Invoice plan not found'}), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'error': str(e)}), content_type='application/json')

    # Tambahkan route ini di controller:
    @http.route('/portal/task/<int:task_id>/sync-bobot', type='http', auth='user', website=True, methods=['POST'])
    def portal_sync_bobot(self, task_id, **kw):
        try:
            flags = self._role_flags()
            if not flags['can_update_task']:
                return Response(json.dumps({'success': False, 'error': 'Tidak diizinkan'}),
                                content_type='application/json')

            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task tidak ditemukan'}),
                                content_type='application/json')

            task.action_bobot_sync()
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'success': False, 'error': str(e)}),
                            content_type='application/json')

    @http.route('/portal/task/<int:task_id>/calculate-bobot', type='http', auth='user', website=True, methods=['POST'])
    def portal_calculate_bobot(self, task_id, **kw):
        try:
            flags = self._role_flags()
            if not flags['can_update_task']:
                return Response(json.dumps({'success': False, 'error': 'Tidak diizinkan'}),
                                content_type='application/json')

            task = request.env['project.task'].sudo().browse(task_id).exists()
            if not task:
                return Response(json.dumps({'success': False, 'error': 'Task tidak ditemukan'}),
                                content_type='application/json')

            task.action_bobot_calc_rate()
            return Response(json.dumps({'success': True}), content_type='application/json')
        except Exception as e:
            return Response(json.dumps({'success': False, 'error': str(e)}), content_type='application/json')