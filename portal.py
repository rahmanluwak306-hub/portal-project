from odoo.addons.portal.controllers.portal import CustomerPortal as Portal
from odoo.addons.portal.controllers.portal import pager, get_error
from odoo.exceptions import AccessDenied, AccessError, MissingError, UserError, ValidationError
import math
import logging
from odoo import http, tools, _, SUPERUSER_ID
from odoo.http import request, Response, route
from datetime import datetime, date, timedelta
from dateutil.relativedelta import relativedelta

_logger = logging.getLogger(__name__)


class WebPortal(Portal):

    @route(['/my', '/my/home'], type='http', auth="user", website=True)
    def home(self, **kw):
        res_users = request.env['res.users'].sudo().browse(request.env.user.id)
        res_groups = request.env['res.groups'].sudo().search([('users','in',res_users.id),('name','=','Portal')], order='id desc', limit=1)
        if res_groups:
            return request.redirect('/portal')
        values = self._prepare_portal_layout_values()
        values.update(self._prepare_home_portal_values([]))
        return request.render("portal.portal_my_home", values)

    def details_form_validate(self, data, partner_creation=False):
        error = dict()
        error_message = []
        # Validation
        for field_name in self._get_mandatory_fields():
            if not data.get(field_name):
                error[field_name] = 'missing'
        # email validation
        if data.get('email') and not tools.single_email_re.match(data.get('email')):
            error["email"] = 'error'
            error_message.append(_('Invalid Email! Please enter a valid email address.'))
        # vat validation
        partner = request.env.user.partner_id
        if data.get("vat") and partner and partner.vat != data.get("vat"):
            # Check the VAT if it is the public user too.
            if partner_creation or partner.can_edit_vat():
                if hasattr(partner, "check_vat"):
                    if data.get("country_id"):
                        data["vat"] = request.env["res.partner"].fix_eu_vat_number(int(data.get("country_id")), data.get("vat"))
                    partner_dummy = partner.new({
                        'vat': data['vat'],
                        'country_id': (int(data['country_id']) if data.get('country_id') else False),
                    })
                    try:
                        partner_dummy.check_vat()
                    except ValidationError as e:
                        error["vat"] = 'error'
                        error_message.append(e.args[0])
            else:
                error_message.append(_('Changing VAT number is not allowed once document(s) have been issued for your account. Please contact us directly for this operation.'))
        # error message for empty required fields
        if [err for err in error.values() if err == 'missing']:
            error_message.append(_('Some required fields are empty.'))
        unknown = [k for k in data if k not in self._get_mandatory_fields() + self._get_optional_fields()]
        if unknown:
            error['common'] = 'Unknown field'
            error_message.append("Unknown field '%s'" % ','.join(unknown))
        return error, error_message

    def _get_mandatory_fields(self):
        return ["name", "phone", "email", "street", "city", "country_id"]

    def _get_optional_fields(self):
        return ["street2", "zipcode", "state_id", "vat", "company_name"]

    def on_account_update(self, values, partner):
        pass

    def _update_password(self, old, new1, new2):
        for k, v in [('old', old), ('new1', new1), ('new2', new2)]:
            if not v:
                return {'errors': {'password': {k: _("You cannot leave any password empty.")}}}
        if new1 != new2:
            return {'errors': {'password': {'new2': _("The new password and its confirmation must be identical.")}}}

        try:
            request.env['res.users'].change_password(old, new1)
        except AccessDenied as e:
            msg = e.args[0]
            if msg == AccessDenied().args[0]:
                msg = _('The old password you provided is incorrect, your password was not changed.')
            return {'errors': {'password': {'old': msg}}}
        except UserError as e:
            return {'errors': {'password': str(e)}}

        # update session token so the user does not get logged out (cache cleared by passwd change)
        new_token = request.env.user._compute_session_token(request.session.sid)
        request.session.session_token = new_token

        return {'success': {'password': True}}

    def _prepare_home_portal_values(self, counters):
        return {}

    def _prepare_portal_layout_values(self):
        # get customer sales rep
        sales_user_sudo = request.env['res.users']
        partner_sudo = request.env.user.partner_id
        if partner_sudo.user_id and not partner_sudo.user_id._is_public():
            sales_user_sudo = partner_sudo.user_id
        else:
            fallback_sales_user = partner_sudo.commercial_partner_id.user_id
            if fallback_sales_user and not fallback_sales_user._is_public():
                sales_user_sudo = fallback_sales_user
        return {
            'sales_user': sales_user_sudo,
            'page_name': 'home',
        }

    @http.route(['/portal'], type='http', auth='user', website=True, methods=['GET', 'POST'])
    def portal_dashboard(self, **kwargs):
        values = self._prepare_portal_layout_values()
        values.update(self._prepare_home_portal_values([]))
        employee_ids = request.env['hr.employee'].sudo().search([('user_id', '=', request.env.user.id)], order='id desc', limit=1)

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
        values['groups_access'] = groups_access

        # ===================== NOTIFICATION =====================
        new_task_count = 0
        if groups_access and groups_access == 'project_admin':
            task_ids = request.env['project.task'].sudo().search([], order='id desc')
            new_task_count = len(task_ids)
        elif groups_access and groups_access in ('project_manager','project_head'):
            task_ids = request.env['project.task'].sudo().search(['|',('z_head_assignes_ids','in',employee_ids.ids),('z_member_assignes_ids','in',employee_ids.ids)], order='id desc')
            new_task_count = len(task_ids)
        elif groups_access and groups_access == 'project_engineer':
            task_ids = request.env['project.task'].sudo().search([('z_member_assignes_ids','in',employee_ids.ids)], order='id desc')
            new_task_count = len(task_ids)
        values['new_task_count'] = new_task_count
        return request.render('z_project.portal_dashboard', values)

    @http.route('/portal/security', type='http', auth='user', website=True, methods=['GET', 'POST'])
    def portal_security(self, **post):
        values = self._prepare_portal_layout_values()
        values['get_error'] = get_error
        values['allow_api_keys'] = bool(request.env['ir.config_parameter'].sudo().get_param('portal.allow_api_keys'))
        values['open_deactivate_modal'] = False
        if request.httprequest.method == 'POST':
            values.update(self._update_password(
                post['old'].strip(),
                post['new1'].strip(),
                post['new2'].strip()
            ))
        return request.render('z_project.portal_security', values, headers={
            'X-Frame-Options': 'SAMEORIGIN',
            'Content-Security-Policy': "frame-ancestors 'self'"
        })

    @http.route(['/portal/account'], type='http', auth='user', website=True)
    def account(self, redirect=None, **post):
        values = self._prepare_portal_layout_values()
        partner = request.env.user.partner_id
        values.update({
            'error': {},
            'error_message': [],
        })
        if post and request.httprequest.method == 'POST':
            if not partner.can_edit_vat():
                post['country_id'] = str(partner.country_id.id)
            error, error_message = self.details_form_validate(post)
            values.update({'error': error, 'error_message': error_message})
            values.update(post)
            if not error:
                values = {key: post[key] for key in self._get_mandatory_fields()}
                values.update({key: post[key] for key in self._get_optional_fields() if key in post})
                for field in set(['country_id', 'state_id']) & set(values.keys()):
                    try:
                        values[field] = int(values[field])
                    except:
                        values[field] = False
                values.update({'zip': values.pop('zipcode', '')})
                self.on_account_update(values, partner)
                partner.sudo().write(values)
                if redirect:
                    return request.redirect(redirect)
                return request.redirect('/portal')
        countries = request.env['res.country'].sudo().search([])
        states = request.env['res.country.state'].sudo().search([])
        values.update({
            'partner': partner,
            'countries': countries,
            'states': states,
            'has_check_vat': hasattr(request.env['res.partner'], 'check_vat'),
            'partner_can_edit_vat': partner.can_edit_vat(),
            'redirect': redirect,
            'page_name': 'my_details',
        })
        response = request.render("z_project.portal_account", values)
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Content-Security-Policy'] = "frame-ancestors 'self'"
        return response
