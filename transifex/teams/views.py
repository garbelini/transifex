# -*- coding: utf-8 -*-
import copy
import itertools
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.contrib import messages
from django.core.urlresolvers import reverse
from django.views.decorators.http import require_POST
from django.db import IntegrityError
from django.db import transaction
from django.db.models import Q, Sum
from django.dispatch import Signal
from django.http import HttpResponseRedirect, HttpResponse, Http404
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext
from django.template.response import TemplateResponse
from django.utils.translation import ugettext as _

from django.utils import simplejson
from djpjax import pjax
import re

from actionlog.models import action_logging
from transifex.languages.models import Language
from notification import models as notification
from transifex.projects.models import Project
from transifex.projects.permissions import *
from transifex.projects.signals import pre_team_request, pre_team_join, ClaNotSignedError
from transifex.resources.models import RLStats, Resource
from transifex.teams.forms import TeamSimpleForm, TeamRequestSimpleForm, ProjectsFilterForm
from transifex.teams.models import Team, TeamAccessRequest, TeamRequest
from transifex.teams import signals as team_signals
# Temporary
from transifex.txcommon import notifications as txnotification

from transifex.txcommon.decorators import one_perm_required_or_403, access_off
from transifex.txcommon.log import logger

def team_off(request, project, *args, **kwargs):
    """
    This view is used by the decorator 'access_off' to redirect a user when
    a project outsources its teams or allow anyone to submit files.

    Usage: '@access_off(team_off)' in front on any team view.
    """
    language_code = kwargs.get('language_code', None)
    if language_code:
        language = Language.objects.by_code_or_alias_or_404(language_code)
        extra_context = {
            'parent_template': 'teams/team_menu.html',
            'language': language,
            'project_team_members': True,
        }
    else:
        extra_context = {
            'parent_template': 'projects/project_menu.html',
            'project_overview': True,
        }

    context = {
        'project': project,
    }

    context.update(extra_context)

    return render_to_response('teams/team_off.html', context,
        context_instance=RequestContext(request)
    )

def update_team_request(team):
    project = team.project
    language = team.language
    try:
        team_request = project.teamrequest_set.get(
                language=language)
        user = team_request.user
        if not (user in team.members.all() or user in team.coordinators.all()\
                or user in team.reviewers.all()):
            team_access_request = TeamAccessRequest.objects.create(
                    user=user, team=team, created=team_request.created)
        team_request.delete()
    except TeamRequest.DoesNotExist, e:
        pass

def _team_create_update(request, project_slug, language_code=None, extra_context=None):
    """
    Handler for creating and updating a team of a project.

    This function helps to eliminate duplication of code between those two
    actions, and also allows to apply different permission checks in the
    respective views.
    """
    project = get_object_or_404(Project, slug=project_slug)
    team, language = None, None

    if language_code:
        language = get_object_or_404(Language, code=language_code)
        try:
            team = Team.objects.get(project__pk=project.pk,
                language=language)
        except Team.DoesNotExist:
            pass

    if request.POST:
        form = TeamSimpleForm(project, language, request.POST, instance=team)
        form.data["creator"] = request.user.pk
        if form.is_valid():
            team=form.save(commit=False)
            team_id = team.id
            team.save()
            form.save_m2m()

            # Delete access requests for users that were added
            for member in itertools.chain(team.members.all(),
                team.coordinators.all()):
                tr = TeamAccessRequest.objects.get_or_none(team, member)
                if tr:
                    tr.delete()

            # ActionLog & Notification
            # TODO: Use signals
            if not team_id:
                nt = 'project_team_added'
            else:
                nt = 'project_team_changed'

            context = {'team': team,
                       'sender': request.user}

            # Logging action
            action_logging(request.user, [project, team], nt, context=context)
            update_team_request(team)

            if settings.ENABLE_NOTICES:
                # Send notification for those that are observing this project
                txnotification.send_observation_notices_for(project,
                        signal=nt, extra_context=context)
                # Send notification for maintainers and coordinators
                from notification.models import NoticeType
                try:
                    notification.send(set(itertools.chain(project.maintainers.all(),
                        team.coordinators.all())), nt, context)
                except NoticeType.DoesNotExist:
                    pass

            return HttpResponseRedirect(reverse("team_members",
                                        args=[project.slug, team.language.code]))
    else:
        form = TeamSimpleForm(project, language, instance=team)

    context = {
        "project": project,
        "team": team,
        "project_team_form": form,
    }

    if extra_context:
        context.update(extra_context)

    return render_to_response("teams/team_form.html", context,
        context_instance=RequestContext(request))


pr_team_add=(("granular", "project_perm.maintain"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_add,
    (Project, "slug__exact", "project_slug"))
def team_create(request, project_slug):
    extra_context = {
        'parent_template': 'projects/base.html',
        'team_create': True
    }
    return _team_create_update(request, project_slug,
        extra_context=extra_context)


pr_team_update=(("granular", "project_perm.coordinate_team"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_update,
    (Project, 'slug__exact', 'project_slug'),
    (Language, "code__exact", "language_code"))
def team_update(request, project_slug, language_code):
    language = Language.objects.by_code_or_alias_or_404(language_code)
    extra_context = {
        'language': language,
        'parent_template': 'teams/team_menu.html',
        'team_update': True
    }
    return _team_create_update(request, project_slug, language_code,
        extra_context=extra_context)


@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=True)
def team_detail(request, project_slug, language_code):
    project = get_object_or_404(Project.objects.select_related(), slug=project_slug)
    language = Language.objects.by_code_or_alias_or_404(language_code)
    team = Team.objects.get_or_none(project, language.code)

    filter_form = ProjectsFilterForm(project, request.GET)

    projects_filter = []
    if filter_form.is_valid():
        projects_filter = filter_form.cleaned_data['project']

    if team and request.user.is_authenticated():
        user_access_request = request.user.teamaccessrequest_set.filter(
            team__pk=team.pk)
    else:
        user_access_request = None

    statslist = RLStats.objects.select_related('resource', 'resource__project',
        'lock', 'last_committer', 'resource__priority')

    if projects_filter:
        statslist = statslist.filter(resource__project__in=[projects_filter,])

    statslist = statslist.by_project_and_language(project, language)

    if not statslist:
        raise Http404

    empty_rlstats = Resource.objects.select_related('project', 'priority'
        ).by_project(project).exclude(id__in=statslist.values('resource')
        ).order_by('project__name')

    if projects_filter:
        empty_rlstats = empty_rlstats.filter(project__in=[projects_filter,])

    total_entries = Resource.objects.by_project(project).aggregate(
        total_entities=Sum('total_entities'))['total_entities']

    if team:
        coordinators = team.coordinators.select_related('profile').all()[:6]
    else:
        coordinators = None

    return render_to_response("teams/team_detail.html", {
        "project": project,
        "language": language,
        "team": team,
        "user_access_request": user_access_request,
        "project_team_page": True,
        "statslist": statslist,
        "empty_rlstats": empty_rlstats,
        "filter_form": filter_form,
        "total_entries": total_entries,
        "coordinators": coordinators,
    }, context_instance=RequestContext(request))

@transaction.commit_on_success
@require_POST
def change_member_type(request, project_slug, language_code, username, member_type):
    """
    Switch 'member type' from the current one to
    - coordinator
    - reviewer
    - translator
    """
    user = get_object_or_404(User, username=username)
    team = get_object_or_404(Team,
        project__slug__exact=project_slug,
        language__code__iexact=language_code)

    request_by_maintainer = team.project.maintainers.filter(
        id=request.user.id).exists()
    success = True

    if member_type == 'translator':
        team.coordinators.remove(user)
        team.reviewers.remove(user)
        team.members.add(user)
    elif member_type == 'reviewer':
        team.members.remove(user)
        team.coordinators.remove(user)
        team.reviewers.add(user)
    elif member_type == 'coordinator' and request_by_maintainer:
        team.reviewers.remove(user)
        team.members.remove(user)
        team.coordinators.add(user)
    else:
        success = False

    response = simplejson.dumps({'success': success})
    return HttpResponse(response)

def _team_members_common_context(request, project_slug, language_code):
    """
    Gathers content common in team_members_{show, edit, whatever}
    """
    username = request.GET.get('username', None)
    project = get_object_or_404(Project.objects.select_related(), slug=project_slug)
    language = get_object_or_404(Language.objects.select_related(), code=language_code)
    team = Team.objects.get_or_none(project, language.code)

    selected_user = None
    if username:
        try:
            selected_user = User.objects.get(username__iexact=username)
        except User.DoesNotExist:
            pass

    return {
        'project': project, 'language': language, 'team': team,
        'selected_user': selected_user,
        'next_url': request.get_full_path(),
        'next_url_clean': re.sub(r'\?[^?]*$', '', request.get_full_path()),
        'project_team_members': True,
    }

@access_off(team_off)
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=True)
@pjax('teams/_user_profile.html')
def team_members_index(request, project_slug, language_code):
    """
    Allows everyone to list the members of a team
    """
    context = _team_members_common_context(request, project_slug, language_code)
    members_filter = request.GET.get('filter', None)

    members = _filter_members(context['team'], members_filter)
    members = members.only('username', 'first_name', 'last_name')
    members = members.order_by('username')

    context.update({'members': members, 'action': 'show'})
    return TemplateResponse(request, 'teams/team_members.html', context)

def _filter_members(team, members_filter):
    """
    Isolating member filtering functionality.
    """
    if members_filter == None:
        members = team.all_members()
    elif members_filter == 'coordinators':
        members = team.coordinators.all()
    elif members_filter == 'reviewers':
        members = team.reviewers.all()
    return members

@access_off(team_off)
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=False)
@pjax('teams/_user_profile.html')
def team_members_edit(request, project_slug, language_code):
    """
    Allows maintainers/coordinators to
    - list the members of their team
    - accept/reject join requests
    - unmember members
    """
    context = _team_members_common_context(request, project_slug, language_code)
    members_filter = request.GET.get('filter', None)
    team = context['team']

    # shouldn't allow /edit?username=moufadios if moufadios not a team member
    if context['selected_user']:
        kwargs = { 'id': context['selected_user'].id }
        if not team.all_members().filter(**kwargs).exists():
            raise Http404

    team_access_requests = TeamAccessRequest.objects.filter(team__pk=team.pk)
    if request.user.is_authenticated():
        rel_manager = request.user.teamaccessrequest_set
        user_access_request = rel_manager.filter(team__pk=team.pk)
    else:
        user_access_request = None

    members = _filter_members(context['team'], members_filter)
    members = members.only('username', 'first_name', 'last_name')
    members = members.order_by('username')

    context.update({
        'members': members,
        'team_access_requests': team_access_requests,
        'user_access_request': user_access_request,
        'action': 'edit',
    })
    return TemplateResponse(request, 'teams/team_members.html', context)

@access_off(team_off)
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'), anonymous_access=False)
@login_required
def team_members_remove(request, project_slug, language_code, username):
    """
    Removes a user from a team.
    """
    user = get_object_or_404(User, username__iexact=username)
    team = get_object_or_404(Team,
        project__slug__exact=project_slug,
        language__code__iexact=language_code)

    username = '%s' % user.username
    if user.first_name or user.last_name:
        full_name = ' '.join([user.first_name, user.last_name]).strip()
        username += ' (%s)' % full_name

    try:
        team.members.remove(user)
        msg = _("User %s was removed from team") % username
        messages.success(request, msg)
        error_msg = None
    except e:
        msg = _("User %s could not be removed from team" % username)
        messages.error(request, msg)
        error_msg = e.message

    team_signals.team_member_removed.send(sender=None,
        error_msg=error_msg, team=team, user=user)

    args = (project_slug, language_code)
    return HttpResponseRedirect(reverse('team_members_edit', args=args))

def _team_members_remove_peripherals(sender, **kwargs):
    """
    Handles functionality of team_members_remove() that could be handled by
    a task queue.
    """
    if kwargs['error_msg']:
        error_msg = "Could not remove %s from team %s: %s" % (
            kwargs['user'], kwargs['team'], kwargs['error_msg'])
        logger.error(error_msg)

team_signals.team_member_removed.connect(_team_members_remove_peripherals)

pr_team_delete=(("granular", "project_perm.maintain"),
                ("general",  "teams.delete_team"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_delete,
    (Project, "slug__exact", "project_slug"))
def team_delete(request, project_slug, language_code):

    project = get_object_or_404(Project, slug=project_slug)
    team = get_object_or_404(Team, project__pk=project.pk,
        language__code=language_code)

    if request.method == "POST":
        _team = copy.copy(team)
        team.delete()
        messages.success(request, _("The team '%s' was deleted.") % _team.language.name)

        # ActionLog & Notification
        # TODO: Use signals
        nt = 'project_team_deleted'
        context = {'team': _team,
                   'sender': request.user}

        #Delete rlstats for this team in outsourced projects
        for p in project.outsourcing.all():
            RLStats.objects.select_related('resource').by_project_and_language(
                    p, _team.language).filter(translated=0).delete()

        # Logging action
        action_logging(request.user, [project, _team], nt, context=context)

        if settings.ENABLE_NOTICES:
            # Send notification for those that are observing this project
            txnotification.send_observation_notices_for(project,
                    signal=nt, extra_context=context)
            # Send notification for maintainers
            notification.send(project.maintainers.all(), nt, context)

        return HttpResponseRedirect(reverse("project_detail",
                                     args=(project_slug,)))
    else:
        return render_to_response(
            "teams/team_confirm_delete.html",
            {"team": team, "project": team.project},
            context_instance=RequestContext(request)
        )


@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'))
@transaction.commit_on_success
def team_join_request(request, project_slug, language_code):

    team = get_object_or_404(Team, project__slug=project_slug,
        language__code=language_code)
    project = team.project

    if request.POST:
        if request.user in team.members.all() or \
            request.user in team.coordinators.all():
            messages.warning(request,
                          _("You are already on the '%s' team.") % team.language.name)
        try:
            # send pre_team_join signal
            cla_sign = 'cla_sign' in request.POST and request.POST['cla_sign']
            cla_sign = cla_sign and True
            pre_team_join.send(sender='join_team_view', project=project,
                               user=request.user, cla_sign=cla_sign)

            access_request = TeamAccessRequest(team=team, user=request.user)
            access_request.save()
            messages.success(request,
                _("You requested to join the '%s' team.") % team.language.name)
            # ActionLog & Notification
            # TODO: Use signals
            nt = 'project_team_join_requested'
            context = {'access_request': access_request,
                       'sender': request.user}

            # Logging action
            action_logging(request.user, [project, team], nt, context=context)

            if settings.ENABLE_NOTICES:
                # Send notification for those that are observing this project
                txnotification.send_observation_notices_for(project,
                        signal=nt, extra_context=context)
                # Send notification for maintainers and coordinators
                notification.send(set(itertools.chain(project.maintainers.all(),
                    team.coordinators.all())), nt, context)


        except IntegrityError:
            transaction.rollback()
            messages.error(request,
                            _("You already requested to join the '%s' team.")
                             % team.language.name)
        except ClaNotSignedError, e:
            messages.error(request,
                             _("You need to sign the Contribution License Agreement for this "\
                "project before you join a translation team"))


    return HttpResponseRedirect(reverse("team_detail",
                                        args=[project_slug, language_code]))



pr_team_add_member_perm=(("granular", "project_perm.coordinate_team"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_add_member_perm,
    (Project, "slug__exact", "project_slug"),
    (Language, "code__exact", "language_code"))
@transaction.commit_on_success
def team_join_approve(request, project_slug, language_code, username):
    if not request.is_ajax() or request.method != "POST":
		return HttpResponseRedirect(reverse("team_detail",
	  		args=[project_slug, language_code]))

    # we can haz ajax post
    team = get_object_or_404(Team, project__slug=project_slug,
        language__code=language_code)
    project = team.project
    user = get_object_or_404(User, username=username)
    access_request = get_object_or_404(TeamAccessRequest, team__pk=team.pk,
        user__pk=user.pk)

    if user in team.members.all() or user in team.coordinators.all():
	access_request.delete()
    try:
	team.members.add(user)
	team.save()
	access_request.delete()
        error_msg = None
    except IntegrityError, e:
	transaction.rollback()
        error_msg = e.message

    team_signals.team_join_approved.send(sender=None,
        nt='project_team_join_approved',
        context = {'access_request':access_request, 'sender':request.user},
        project=project, team=team, access_request=access_request,
        error_msg=error_msg)

    success = False if error_msg else True
    response = {'user_id':user.id, 'success':success, 'accepted':True}
    return HttpResponse(simplejson.dumps(response))

pr_team_deny_member_perm=(("granular", "project_perm.coordinate_team"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_deny_member_perm,
    (Project, "slug__exact", "project_slug"),
    (Language, "code__exact", "language_code"))
@transaction.commit_on_success
def team_join_deny(request, project_slug, language_code, username):
    if not request.is_ajax() or request.method != "POST":
	return HttpResponseRedirect(reverse("team_detail",
	  args=[project_slug, language_code]))

    # we (the cats) can haz ajax post
    team = get_object_or_404(Team, project__slug=project_slug,
        language__code=language_code)
    project = team.project
    user = get_object_or_404(User, username=username)
    access_request = get_object_or_404(TeamAccessRequest, team__pk=team.pk,
        user__pk=user.pk)

    try:
	access_request.delete()
        error_msg = None
    except IntegrityError, e:
	transaction.rollback()
        error_msg = e.message

    team_signals.team_join_denied.send(sender=None,
        nt='project_team_join_denied',
        context={
            'access_request':access_request,
            'performer': request.user,
            'sender': request.user},
        project=project, team=team, access_request=access_request,
        error_msg=error_msg)

    success = False if error_msg else True
    response = {'user_id':user.id, 'success':success, 'accepted':False}
    return HttpResponse(simplejson.dumps(response))

def _team_join_action_notify(access_request, project, team, nt, context):
    """
    Send notifications when a user's request to join a team gets
    accepted/rejected.

    Called by _team_join_action_peripherals.
    """
    # Send notification for those that are observing this project
    txnotification.send_observation_notices_for(project,
	    signal=nt, extra_context=context)
    # Send notification for maintainers, coordinators and the user
    notification.send(set(itertools.chain(project.maintainers.all(),
	team.coordinators.all(), [access_request.user])), nt, context)

def _team_join_action_peripherals(sender, **kwargs):
    """
    Takes care of all the tasks that can be forwarded to a task queue.
    """
    # yeah, I know it's 'cheap' but gimme a break.
    nt, context = kwargs['nt'], kwargs['context']
    project, team = kwargs['project'], kwargs['team']
    access_request = kwargs['access_request']
    request_user = context['sender']
    error_msg = kwargs['error_msg']

    if error_msg:
	logger.error("Something weird happened: %s" % error_msg)
        return

    # user's request to join team was successfully accepted or rejected.
    action_logging(request_user, [project, team], nt, context=context)
    if settings.ENABLE_NOTICES:
       _team_join_action_notify(access_request, project, team, nt, context)

team_signals.team_join_approved.connect(_team_join_action_peripherals)
team_signals.team_join_denied.connect(_team_join_action_peripherals)

@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'))
@transaction.commit_on_success
def team_join_withdraw(request, project_slug, language_code):

    team = get_object_or_404(Team, project__slug=project_slug,
        language__code=language_code)
    project = team.project
    access_request = get_object_or_404(TeamAccessRequest, team__pk=team.pk,
        user__pk=request.user.pk)

    if request.POST:
        try:
            access_request.delete()
            messages.success(request,_(
                "You withdrew your request to join the '%s' team."
                ) % team.language.name)

            # ActionLog & Notification
            # TODO: Use signals
            nt = 'project_team_join_withdrawn'
            context = {'access_request': access_request,
                       'performer': request.user,
                       'sender': request.user}

            # Logging action
            action_logging(request.user, [project, team], nt, context=context)

            if settings.ENABLE_NOTICES:
                # Send notification for those that are observing this project
                txnotification.send_observation_notices_for(project,
                        signal=nt, extra_context=context)
                # Send notification for maintainers, coordinators
                notification.send(set(itertools.chain(project.maintainers.all(),
                    team.coordinators.all())), nt, context)

        except IntegrityError, e:
            transaction.rollback()
            logger.error("Something weird happened: %s" % str(e))

    return HttpResponseRedirect(reverse("team_detail",
                                        args=[project_slug, language_code]))

@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'))
@transaction.commit_on_success
def team_leave(request, project_slug, language_code):

    team = get_object_or_404(Team, project__slug=project_slug,
        language__code=language_code)
    project = team.project

    if request.POST:
        try:
            if (team.members.filter(username=request.user.username).exists() or
                team.reviewers.filter(username=request.user.username).exists()):
                team.members.remove(request.user)
                team.reviewers.remove(request.user)
                messages.info(request, _(
                    "You left the '%s' team."
                    ) % team.language.name)

                # ActionLog & Notification
                # TODO: Use signals
                nt = 'project_team_left'
                context = {'team': team,
                           'performer': request.user,
                           'sender': request.user}

                # Logging action
                action_logging(request.user, [project, team], nt, context=context)

                if settings.ENABLE_NOTICES:
                    # Send notification for those that are observing this project
                    txnotification.send_observation_notices_for(project,
                            signal=nt, extra_context=context)
                    # Send notification for maintainers, coordinators
                    notification.send(set(itertools.chain(project.maintainers.all(),
                        team.coordinators.all())), nt, context)
            else:
                messages.info(request, _(
                    "You are not in the '%s' team."
                    ) % team.language.name)

        except IntegrityError, e:
            transaction.rollback()
            logger.error("Something weird happened: %s" % str(e))

    return HttpResponseRedirect(reverse("team_detail",
                                        args=[project_slug, language_code]))


# Team Creation
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_project_private_perm,
    (Project, 'slug__exact', 'project_slug'))
@transaction.commit_on_success
def team_request(request, project_slug):

    if request.POST:
        language_pk = request.POST.get('language', None)
        if not language_pk:
            messages.error(request, _(
                "Please select a language before submitting the form."))
            return HttpResponseRedirect(reverse("project_detail",
                                        args=[project_slug,]))


        project = get_object_or_404(Project, slug=project_slug)

        language = get_object_or_404(Language, pk=int(language_pk))

        try:
            team = Team.objects.get(project__pk=project.pk,
                language__pk=language.pk)
            messages.warning(request,_(
                "'%s' team already exists.") % team.language.name)
        except Team.DoesNotExist:
            try:
                team_request = TeamRequest.objects.get(project__pk=project.pk,
                    language__pk=language.pk)
                messages.warning(request, _(
                    "A request to create the '%s' team already exists.")
                    % team_request.language.name)
            except TeamRequest.DoesNotExist:
                try:
                    # send pre_team_request signal
                    cla_sign = 'cla_sign' in request.POST and \
                            request.POST['cla_sign']
                    cla_sign = cla_sign and True
                    pre_team_request.send(sender='request_team_view',
                                          project=project,
                                          user=request.user,
                                          cla_sign=cla_sign)

                    team_request = TeamRequest(project=project,
                        language=language, user=request.user)
                    team_request.save()
                    messages.info(request, _(
                        "You requested creation of the '%s' team.")
                        % team_request.language.name)

                    # ActionLog & Notification
                    # TODO: Use signals
                    nt = 'project_team_requested'
                    context = {'team_request': team_request,
                               'sender': request.user}

                    # Logging action
                    action_logging(request.user, [project], nt, context=context)

                    if settings.ENABLE_NOTICES:
                        # Send notification for those that are observing this project
                        txnotification.send_observation_notices_for(project,
                                signal=nt, extra_context=context)
                        # Send notification for maintainers
                        notification.send(project.maintainers.all(), nt, context)

                except IntegrityError, e:
                    transaction.rollback()
                    logger.error("Something weird happened: %s" % str(e))
                except ClaNotSignedError, e:
                    messages.error(request, _(
                        "You need to sign the Contribution License Agreement "\
                        "for this project before you submit a team creation "\
                        "request."
                    ))

    return HttpResponseRedirect(reverse("project_detail", args=[project_slug,]))


pr_team_request_approve=(("granular", "project_perm.maintain"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_request_approve,
    (Project, "slug__exact", "project_slug"),)
@transaction.commit_on_success
def team_request_approve(request, project_slug, language_code):

    team_request = get_object_or_404(TeamRequest, project__slug=project_slug,
        language__code=language_code)
    project = team_request.project

    if request.POST:
        try:
            team = Team(project=team_request.project,
                language=team_request.language, creator=request.user)
            team.save()
            team.coordinators.add(team_request.user)
            team.save()
            team_request.delete()
            messages.success(request, _(
                "You approved the '%(team)s' team requested by '%(user)s'."
                ) % {'team':team.language.name, 'user':team_request.user})

            # ActionLog & Notification
            # TODO: Use signals
            nt = 'project_team_added'
            context = {'team': team,
                       'sender': request.user}

            # Logging action
            action_logging(request.user, [project, team], nt, context=context)

            if settings.ENABLE_NOTICES:
                # Send notification for those that are observing this project
                txnotification.send_observation_notices_for(project,
                        signal=nt, extra_context=context)
                # Send notification for maintainers and coordinators
                notification.send(set(itertools.chain(project.maintainers.all(),
                    team.coordinators.all())), nt, context)

        except IntegrityError, e:
            transaction.rollback()
            logger.error("Something weird happened: %s" % str(e))

    return HttpResponseRedirect(reverse("project_detail",
                                        args=[project_slug,]))


pr_team_request_deny=(("granular", "project_perm.maintain"),)
@access_off(team_off)
@login_required
@one_perm_required_or_403(pr_team_request_deny,
    (Project, "slug__exact", "project_slug"),)
@transaction.commit_on_success
def team_request_deny(request, project_slug, language_code):

    team_request = get_object_or_404(TeamRequest, project__slug=project_slug,
        language__code=language_code)
    project = team_request.project

    if request.POST:
        try:
            team_request.delete()
            messages.success(request, _(
                "You rejected the request by '%(user)s' for a '%(team)s' team."
                ) % {'team':team_request.language.name,
                     'user':team_request.user})

            # ActionLog & Notification
            # TODO: Use signals
            nt = 'project_team_request_denied'
            context = {'team_request': team_request,
                       'performer': request.user,
                       'sender': request.user}

            # Logging action
            action_logging(request.user, [project], nt, context=context)

            if settings.ENABLE_NOTICES:
                # Send notification for those that are observing this project
                txnotification.send_observation_notices_for(project,
                        signal=nt, extra_context=context)
                # Send notification for maintainers and the user
                notification.send(set(itertools.chain(project.maintainers.all(),
                    [team_request.user])), nt, context)

        except IntegrityError, e:
            transaction.rollback()
            logger.error("Something weird happened: %s" % str(e))

    return HttpResponseRedirect(reverse("project_detail",
                                        args=[project_slug,]))

