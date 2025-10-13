from odoo.addons.portal.controllers.portal import CustomerPortal, pager, get_error
from odoo.exceptions import AccessDenied, AccessError, MissingError, UserError, ValidationError
from odoo import http, tools, _, SUPERUSER_ID
from odoo.http import request, Response
from datetime import datetime, date, timedelta
from html import unescape
from urllib.parse import urlencode
import logging
import math
import re

_logger = logging.getLogger(__name__)


class PortalTimesheetControllers(http.Controller):

    def parse_from_data(self, data_dict, key_timesheets=None):
        for key in key_timesheets:
            data_dict.pop(key, None)
        for key, value in data_dict.items():
            if value and len(value) == 1 and not '_ids' in key:
                value = value[0]
            if value and '_id' in key and not '_ids' in key:
                value = int(value)
            elif value and '_ids' in key:
                data_array = []
                if len(value) == 1 and value[0]:
                    data_array.append(int(value[0]))
                elif len(value) > 1:
                    data_array = [int(v) for v in value if v]
                value = [(6, 0, data_array)]
            elif value and 'date' in key and type(key) == 'datetime':
                value = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            elif value and 'date' in key and type(key) == 'date':
                value = datetime.strptime(value, '%Y-%m-%d').date()
            if not value and type(key) == 'list':
                value = [(6, 0, [])]
            elif not value and not type(key) == 'list':
                value = False
            data_dict[key] = value
        return data_dict

    @http.route([
        '/portal/timesheets',
        '/portal/timesheets/<int:timesheet_id>',
        '/portal/timesheets/corrections/<int:correction_id>',
        '/portal/timesheets/page/<int:page>',
    ], type='http', auth='user', website=True, methods=['GET', 'POST'])
    def portal_timesheets(self, page=1, timesheet_id=None, correction_id=None, view_type=None, model=None, function=None, filterby='', search='', sortby='terbaru', groupby='', **kw):
        step = 20
        group_step = 20
        domain = []
        url = '/portal/timesheets'
        if not timesheet_id:
            timesheet_id = kw.get('timesheet_id')
        if not correction_id:
            correction_id = kw.get('correction_id')

        # ===================== SORT & GROUP CONFIG =====================
        view_type_settings = {
            'account.analytic.line': {'label': 'Request Timesheets'},
            'account.analytic.line.request': {'label': 'Correction Timesheets'},
        }
        searchbar_group = {}
        if not model:
            model = 'account.analytic.line'
        if view_type_settings[model]['label'] == 'Request Timesheets':
            searchbar_filter = {
                '': {'label': 'None', 'domain': []},
                'Draft': {'label': 'Draft', 'domain': [('z_state', '=', 'draft')]},
                'Approval': {'label': 'Approval', 'domain': [('z_state', '=', 'waiting_approval')]},
                'Approved': {'label': 'Approved', 'domain': [('z_state', '=', 'approved')]},
                'Rejected': {'label': 'Rejected', 'domain': [('z_state', '=', 'reject')]},
            }
            searchbar_sort = {
                'terbaru': {'label': 'Terbaru', 'order': 'z_timesheet_start_date desc'},
                'terlama': {'label': 'Terlama', 'order': 'z_timesheet_start_date asc'},
                'employee': {'label': 'Employee', 'order': 'employee_id asc'},
                'Projects': {'label': 'Project', 'order': 'project_id desc'},
                'Tasks': {'label': 'Tasks', 'order': 'task_id desc'},
            }
            searchbar_group = {
                '': {'label': 'None'},
                'partner_id': {'label': 'Customer'},
                'task_id': {'label': 'Tasks'},
                'project_id': {'label': 'Projects'},
                'employee_id': {'label': 'Employee'},
                'z_state': {'label': 'Status'},
            }
        elif view_type_settings[model]['label'] == 'Correction Timesheets':
            searchbar_filter = {
                '': {'label': 'None', 'domain': []},
                'Draft': {'label': 'Draft', 'domain': [('z_state', '=', 'draft')]},
                'Approval': {'label': 'Approval', 'domain': [('z_state', '=', 'waiting_approval')]},
                'Approved': {'label': 'Approved', 'domain': [('z_state', '=', 'approved')]},
                'Rejected': {'label': 'Rejected', 'domain': [('z_state', '=', 'rejected')]},
            }
            searchbar_sort = {
                'terbaru': {'label': 'Terbaru', 'order': 'z_current_start_date desc'},
                'terlama': {'label': 'Terlama', 'order': 'z_current_start_date asc'},
                'employee': {'label': 'Employee', 'order': 'z_employee_id asc'},
            }
            searchbar_group = {
                '': {'label': 'None'},
                'z_employee_id': {'label': 'Employee'},
                'z_state': {'label': 'Status'},
            }
        sort_field = searchbar_sort[sortby]['order'] if sortby in searchbar_sort else 'id desc'
        filter_field = searchbar_filter[filterby]['domain'] if filterby in searchbar_filter else []
        if view_type_settings[model]['label'] == 'Request Timesheets' and search:
            domain += [
                '|', '|', '|', '|', '|', '|',
                ('name', 'ilike', search),
                ('task_id.partner_id.name', 'ilike', search),
                ('employee_id.name', 'ilike', search),
                ('project_id.name', 'ilike', search),
                ('task_id.name', 'ilike', search),
                ('task_id.z_master_task_id.z_name', 'ilike', search),
                ('z_state', 'ilike', search)
            ]
        elif view_type_settings[model]['label'] == 'Correction Timesheets' and search:
            domain += [
                '|', '|', '|', '|', '|', '|',
                ('z_name', 'ilike', search),
                ('z_timesheet_id.task_id.partner_id.name', 'ilike', search),
                ('z_employee_id.name', 'ilike', search),
                ('z_timesheet_id.project_id.name', 'ilike', search),
                ('z_timesheet_id.task_id.name', 'ilike', search),
                ('z_timesheet_id.task_id.z_master_task_id.z_name', 'ilike', search),
                ('z_state', 'ilike', search)
            ]
        if filter_field:
            domain = filter_field
        if groupby not in searchbar_group:
            groupby = ''
        if timesheet_id and not view_type and view_type_settings[model]['label'] == 'Correction Timesheets':
            domain = [('z_timesheet_id','=',timesheet_id)]

        # ===================== METHOD POST =================
        if request.httprequest.method == 'POST':
            record_id = False
            if view_type_settings[model]['label'] == 'Request Timesheets':
                record_id = timesheet_id
            elif view_type_settings[model]['label'] == 'Correction Timesheets':
                record_id = correction_id
            if record_id and function in ('approved','rejected'):
                record_ids = request.env[model].sudo().search([('id','=',record_id)], order='id desc', limit=1)
                if record_ids:
                    if function and function == 'approved':
                        record_ids.sudo().action_approve()
                    elif function and function == 'rejected':
                        record_ids.sudo().action_reject()
                return request.make_json_response({
                    'success': True,
                    'updated_by': record_ids.write_uid.name or '',
                    'updated_date': record_ids.write_date.strftime('%Y-%m-%d') if record_ids.write_date else ''
                })
            key_timesheets = [
                'csrf_token',
                'project_id',
            ]
            data_text_api = request.httprequest.form
            data_dict = data_text_api.to_dict(flat=False)
            data_dict = self.parse_from_data(data_dict, key_timesheets=key_timesheets)
            data_dict['z_current_start_date'] = datetime.strptime(data_dict['z_current_start_date'], '%Y-%m-%dT%H:%M') - timedelta(hours=7)
            data_dict['z_current_end_date'] = datetime.strptime(data_dict['z_current_end_date'], '%Y-%m-%dT%H:%M') - timedelta(hours=7)
            if 'model' in data_dict:
                model = data_dict['model']
            if view_type_settings[model]['label'] == 'Correction Timesheets' and not function:
                if data_dict and data_dict['timesheet_id']:
                    timesheet_ids = request.env['account.analytic.line'].sudo().search([('id', '=', data_dict['timesheet_id'])], order='id desc', limit=1)
                    correction_ids = request.env[model].sudo().create({
                        'z_timesheet_id': timesheet_ids.id,
                        'z_employee_id': timesheet_ids.employee_id.id,
                        'z_name': data_dict['name'],
                        'z_current_start_date': data_dict['z_current_start_date'],
                        'z_current_end_date': data_dict['z_current_end_date'],
                    })
                    correction_ids.sudo()._getCurrentTimeSpent()
                    return request.redirect(f'/portal/timesheets/{timesheet_ids.id}?view_type=form')

        # ===================== GROUP ACCESS =====================
        groups_access = False
        groups_user = request.env.user.z_project_group_id
        if groups_user:
            group_map = {
                'Projects: Administrator': 'project_admin',
                'Projects: Project Manager': 'project_manager',
                'Projects: Head': 'project_head',
                'Projects: Engineer': 'project_engineer',
                'Projects: Readonly': 'project_readonly',
            }
            groups_access = group_map.get(groups_user.name, False)

        # ===================== CONTENT =====================
        corrections = []
        timesheets = []
        data_employee = request.env['hr.employee'].sudo().search([], order='id desc')
        data_project = request.env['project.project'].sudo().search([], order='id desc')
        data_task = request.env['project.task'].sudo().search([], order='id desc')
        employee_ids = request.env['hr.employee'].sudo().search([('user_id','=',request.env.user.id)], order='id desc', limit=1)
        employee_child_ids = request.env['hr.employee'].sudo().search([('parent_id','=',employee_ids.id)], order='id desc')
        data_employee_content = employee_ids.ids + employee_child_ids.ids
        if view_type_settings[model]['label'] == 'Request Timesheets':
            if groups_access == 'project_head' and employee_ids:
                domain += [('employee_id','in',data_employee_content)]
            if groups_access == 'project_engineer' and employee_ids:
                domain += [('employee_id','=',employee_ids.id)]
            total_timesheets = request.env[model].sudo().search_count(domain)
            pager_details = pager(url=url, total=total_timesheets, page=page, step=step, scope=3, url_args={'filterby': filterby, 'search': search, 'sortby': sortby, 'groupby': groupby})
            timesheets = request.env[model].sudo().search(domain, offset=pager_details['offset'], limit=step, order=sort_field)
        elif view_type_settings[model]['label'] == 'Correction Timesheets':
            if groups_access == 'project_head' and employee_ids:
                domain += [('z_employee_id','in',data_employee_content)]
            if groups_access == 'project_engineer' and employee_ids:
                domain += [('z_employee_id','=',employee_ids.id)]
            total_corrections = request.env[model].sudo().search_count(domain)
            pager_details = pager(url=url, total=total_corrections, page=page, step=step, scope=3, url_args={'filterby': filterby, 'search': search, 'sortby': sortby, 'groupby': groupby})
            corrections = request.env[model].sudo().search(domain, offset=pager_details['offset'], limit=step, order=sort_field)
        if view_type and view_type == 'form' and timesheet_id:
            domain = [('id','=',timesheet_id)]
            timesheets = request.env[model].sudo().search(domain, order='id desc', limit=1)
            timesheets._portal_ensure_token()

        # ===================== GROUP BY + PAGINATION =====================
        if view_type_settings[model]['label'] == 'Request Timesheets':
            if groupby:
                group_field_name = groupby
                read_groups = request.env[model].sudo().read_group(
                    domain,
                    fields=[group_field_name],
                    groupby=[group_field_name],
                    orderby=group_field_name,
                    limit=None
                )
                url_args = dict(kw or {})
                for k in list(url_args.keys()):
                    if k.startswith("group_page_"):
                        url_args.pop(k)
                url_args.update({
                    'filterby': filterby,
                    'search': search,
                    'sortby': sortby,
                    'groupby': groupby,
                })
                common_query = urlencode(url_args, doseq=True)
                data_project_on_group = []
                timesheet = request.env[model].sudo()
                field_obj = timesheet._fields.get(group_field_name)
                group_keys = [g[group_field_name][0] if g[group_field_name] else False for g in read_groups]
                if field_obj.type == 'selection':
                    group_keys = [g[group_field_name] if g[group_field_name] else False for g in read_groups]
                for idx, key in enumerate(group_keys):
                    group_domain = list(domain)
                    if key:
                        group_domain.append((group_field_name, '=', key))
                    else:
                        group_domain.append((group_field_name, '=', False))
                    timesheets_all = timesheet.search(group_domain, order=sort_field)
                    page_param = f'group_page_{idx}'
                    try:
                        page_for_group = int(request.httprequest.args.get(page_param, 1))
                        if page_for_group < 1:
                            page_for_group = 1
                    except Exception:
                        page_for_group = 1
                    offset = (page_for_group - 1) * group_step
                    timesheets_paged = timesheets_all[offset: offset + group_step]
                    total_items = len(timesheets_all)
                    total_pages = (total_items + group_step - 1) // group_step
                    pages = list(range(1, total_pages + 1))
                    if field_obj:
                        if field_obj.comodel_name:
                            group_value = request.env[field_obj.comodel_name].sudo().browse(key) if key else 'Undefined'
                            group_key = group_value
                        elif field_obj.type == 'selection':
                            selection_dict = dict(field_obj.selection)
                            group_key = key
                            group_value = selection_dict.get(key, 'No Status')
                        else:
                            group_value = key or 'Undefined'
                            group_key = group_value
                    else:
                        group_value = key or 'Undefined'
                        group_key = group_value
                    pager_group = {
                        'page_param': page_param,
                        'page': page_for_group,
                        'step': group_step,
                        'offset': offset,
                        'total': total_items,
                        'pages': pages,
                    }
                    data_project_on_group.append({
                        'group_key': group_key,
                        'group_value': group_value,
                        'timesheets': timesheets_paged,
                        'pager': pager_group,
                        'group_index': idx,
                    })
                timesheets = data_project_on_group
            else:
                common_query = urlencode({'filterby': filterby, 'search': search, 'sortby': sortby, 'groupby': groupby}, doseq=True)
        elif not model or model and view_type_settings[model]['label'] == 'Correction Timesheets':
            if groupby:
                group_field_name = groupby
                read_groups = request.env[model].sudo().read_group(
                    domain,
                    fields=[group_field_name],
                    groupby=[group_field_name],
                    orderby=group_field_name,
                    limit=None
                )
                url_args = dict(kw or {})
                for k in list(url_args.keys()):
                    if k.startswith("group_page_"):
                        url_args.pop(k)
                url_args.update({
                    'filterby': filterby,
                    'search': search,
                    'sortby': sortby,
                    'groupby': groupby,
                })
                common_query = urlencode(url_args, doseq=True)
                data_project_on_group = []
                timesheet = request.env[model].sudo()
                field_obj = timesheet._fields.get(group_field_name)
                group_keys = [g[group_field_name][0] if g[group_field_name] else False for g in read_groups]
                if field_obj.type == 'selection':
                    group_keys = [g[group_field_name] if g[group_field_name] else False for g in read_groups]
                for idx, key in enumerate(group_keys):
                    group_domain = list(domain)
                    if key:
                        group_domain.append((group_field_name, '=', key))
                    else:
                        group_domain.append((group_field_name, '=', False))
                    timesheets_all = timesheet.search(group_domain, order=sort_field)
                    page_param = f'group_page_{idx}'
                    try:
                        page_for_group = int(request.httprequest.args.get(page_param, 1))
                        if page_for_group < 1:
                            page_for_group = 1
                    except Exception:
                        page_for_group = 1
                    offset = (page_for_group - 1) * group_step
                    timesheets_paged = timesheets_all[offset: offset + group_step]
                    total_items = len(timesheets_all)
                    total_pages = (total_items + group_step - 1) // group_step
                    pages = list(range(1, total_pages + 1))
                    if field_obj:
                        if field_obj.comodel_name:
                            group_value = request.env[field_obj.comodel_name].sudo().browse(key) if key else 'Undefined'
                            group_key = group_value
                        elif field_obj.type == 'selection':
                            selection_dict = dict(field_obj.selection)
                            group_key = key
                            group_value = selection_dict.get(key, 'No Status')
                        else:
                            group_value = key or 'Undefined'
                            group_key = group_value
                    else:
                        group_value = key or 'Undefined'
                        group_key = group_value
                    pager_group = {
                        'page_param': page_param,
                        'page': page_for_group,
                        'step': group_step,
                        'offset': offset,
                        'total': total_items,
                        'pages': pages,
                    }
                    data_project_on_group.append({
                        'group_key': group_key,
                        'group_value': group_value,
                        'corrections': timesheets_paged,
                        'pager': pager_group,
                        'group_index': idx,
                    })
                corrections = data_project_on_group
            else:
                common_query = urlencode({'filterby': filterby, 'search': search, 'sortby': sortby, 'groupby': groupby}, doseq=True)

        record_number = 0
        if view_type_settings[model]['label'] == 'Request Timesheets':
            record_number += len(timesheets)
        elif view_type_settings[model]['label'] == 'Correction Timesheets':
            record_number += len(corrections)
        values = {
            # filter
            'page_name': 'Timesheets',
            'groups_access': groups_access,
            'record_number': record_number,
            'search': search,
            'filterby': filterby,
            'sortby': sortby,
            'groupby': groupby,
            'searchbar_filter': searchbar_filter,
            'searchbar_sort': searchbar_sort,
            'searchbar_group': searchbar_group,
            'pager_header': pager_details,
            'common_query': common_query,
            # view
            'view_type': view_type,
            'model': model,
            'view_type_settings': view_type_settings,
            # data
            'timesheets': timesheets,
            'corrections': corrections,
            'employee_ids': employee_ids,
            'data_employee_content': data_employee_content,
            'data_employee': data_employee,
            'data_project': data_project,
            'data_task': data_task,
            'kw': kw,
        }
        return request.render('z_project.portal_timesheet', values)