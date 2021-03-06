import itertools
import logging
import os
import shutil
from operator import itemgetter
from random import randrange

from django.conf import settings
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.urlresolvers import reverse
from django.db import IntegrityError
from django.db.models import Count, Q, F, Prefetch
from django.db.utils import ProgrammingError
from django.http import Http404, HttpResponseRedirect, HttpResponse, HttpResponseForbidden
from django.shortcuts import render, get_object_or_404
from django.template.loader import get_template
from django.utils import translation
from django.utils.functional import cached_property
from django.utils.translation import ugettext as _, ugettext_lazy
from django.views.decorators.http import require_POST
from django.views.generic import ListView, View
from django.views.generic.base import TemplateResponseMixin
from django.views.generic.detail import SingleObjectMixin

from django_ace.widgets import ACE_URL
from judge.comments import CommentedDetailView
from judge.forms import ProblemSubmitForm
from judge.models import Problem, Submission, ContestSubmission, ContestProblem, Language, ProblemGroup, Solution, \
    ProblemTranslation, TranslatedProblemForeignKeyQuerySet, RuntimeVersion, ProblemType
from judge.pdf_problems import HAS_PDF, DefaultPdfMaker
from judge.utils.diggpaginator import DiggPaginator
from judge.utils.problems import contest_completed_ids, user_completed_ids, contest_attempted_ids, user_attempted_ids
from judge.utils.views import LoadSelect2Mixin, TitleMixin, generic_message, QueryStringSortMixin


def get_contest_problem(problem, profile):
    try:
        return problem.contests.get(contest_id=profile.current_contest.contest_id)
    except ObjectDoesNotExist:
        return None


class ProblemMixin(object):
    model = Problem
    slug_url_kwarg = 'problem'
    slug_field = 'code'

    def get_object(self, queryset=None):
        problem = super(ProblemMixin, self).get_object(queryset)
        if not problem.is_accessible_by(self.request.user):
            raise Http404()
        return problem

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemMixin, self).get(request, *args, **kwargs)
        except Http404:
            code = kwargs.get(self.slug_url_kwarg, None)
            return generic_message(request, _('No such problem'),
                                   _('Could not find a problem with the code "%s".') % code, status=404)


class ProblemRaw(ProblemMixin, TitleMixin, TemplateResponseMixin, SingleObjectMixin, View):
    context_object_name = 'problem'
    template_name = 'problem/raw.jade'

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(ProblemRaw, self).get_context_data(**kwargs)
        context['problem_name'] = self.object.name
        context['description'] = self.object.description
        return context

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        with translation.override(settings.LANGUAGE_CODE):
            return self.render_to_response(self.get_context_data(
                object=self.object,
            ))


class ProblemDetail(ProblemMixin, CommentedDetailView):
    context_object_name = 'problem'
    template_name = 'problem/problem.jade'

    def get_comment_page(self):
        return 'p:%s' % self.object.code

    def get_context_data(self, **kwargs):
        context = super(ProblemDetail, self).get_context_data(**kwargs)
        user = self.request.user
        authed = user.is_authenticated()
        context['has_submissions'] = authed and Submission.objects.filter(user=user.profile).exists()
        context['contest_problem'] = (None if not authed or user.profile.current_contest is None else
                                      get_contest_problem(self.object, user.profile))
        context['show_languages'] = self.object.allowed_languages.count() != Language.objects.count()
        context['has_pdf_render'] = HAS_PDF
        try:
            context['editorial'] = Solution.objects.get(problem=self.object)
        except ObjectDoesNotExist:
            pass
        try:
            translation = self.object.translations.get(language=self.request.LANGUAGE_CODE)
        except ProblemTranslation.DoesNotExist:
            context['title'] = self.object.name
            context['language'] = settings.LANGUAGE_CODE
            context['description'] = self.object.description
            context['translated'] = False
        else:
            context['title'] = translation.name
            context['language'] = self.request.LANGUAGE_CODE
            context['description'] = translation.description
            context['translated'] = True
        return context


class LatexError(Exception):
    pass


class ProblemPdfView(ProblemMixin, SingleObjectMixin, View):
    logger = logging.getLogger('judge.problem.pdf')
    languages = set(map(itemgetter(0), settings.LANGUAGES))

    def get(self, request, *args, **kwargs):
        if not HAS_PDF:
            raise Http404()

        language = kwargs.get('language', self.request.LANGUAGE_CODE)
        if language not in self.languages:
            raise Http404()

        problem = self.get_object()
        try:
            trans = problem.translations.get(language=language)
        except ProblemTranslation.DoesNotExist:
            trans = None

        error_cache = os.path.join(settings.PROBLEM_PDF_CACHE, '%s.%s.log' % (problem.code, language))
        cache = os.path.join(settings.PROBLEM_PDF_CACHE, '%s.%s.pdf' % (problem.code, language))

        if os.path.exists(error_cache):
            with open(error_cache) as f:
                return HttpResponse(f.read(), status=500, content_type='text/plain')

        if not os.path.exists(cache):
            self.logger.info('Rendering: %s.%s.pdf', problem.code, language)
            with DefaultPdfMaker() as maker, translation.override(language):
                maker.html = get_template('problem/raw.jade').render({
                    'problem': problem,
                    'problem_name': problem.name if trans is None else trans.name,
                    'description': problem.description if trans is None else trans.description,
                    'url': request.build_absolute_uri()
                }).replace('"//', '"http://').replace("'//", "'http://")

                for file in ('style.css', 'pygment-github.css', 'mathjax_config.js'):
                    maker.load(file, os.path.join(settings.DMOJ_RESOURCES, file))
                maker.make()
                if not maker.success:
                    with open(error_cache, 'wb') as f:
                        f.write(maker.log)
                    self.logger.error('Failed to render PDF for %s', problem.code)
                    return HttpResponse(maker.log, status=500, content_type='text/plain')
                shutil.move(maker.pdffile, cache)

        response = HttpResponse()
        if hasattr(settings, 'PROBLEM_PDF_INTERNAL') and request.META.get('SERVER_SOFTWARE', '').startswith('nginx/'):
            response['X-Accel-Redirect'] = '%s/%s.%s.pdf' % (settings.PROBLEM_PDF_INTERNAL, problem.code, language)
        else:
            with open(cache, 'rb') as f:
                response.content = f.read()

        response['Content-Type'] = 'application/pdf'
        response['Content-Disposition'] = 'inline; filename=%s.%s.pdf' % (problem.code, language)
        return response


class ProblemList(QueryStringSortMixin, LoadSelect2Mixin, TitleMixin, ListView):
    model = Problem
    title = ugettext_lazy('Problems')
    context_object_name = 'problems'
    template_name = 'problem/list.jade'
    paginate_by = 50
    sql_sort = frozenset(('points', 'ac_rate', 'user_count', 'code'))
    manual_sort = frozenset(('name', 'group', 'solved', 'type'))
    all_sorts = sql_sort | manual_sort
    default_desc = frozenset(('points', 'ac_rate', 'user_count'))
    default_sort = 'code'

    def get_paginator(self, queryset, per_page, orphans=0,
                      allow_empty_first_page=True, **kwargs):
        paginator = DiggPaginator(queryset, per_page, body=6, padding=2, orphans=orphans,
                                  allow_empty_first_page=allow_empty_first_page, **kwargs)
        if not self.in_contest:
            # Get the number of pages and then add in this magic.
            # noinspection PyStatementEffect
            paginator.num_pages

            queryset = queryset.add_i18n_name(self.request.LANGUAGE_CODE)
            sort_key = self.order.lstrip('-')
            if sort_key in self.sql_sort:
                queryset = queryset.order_by(self.order)
            elif sort_key == 'name':
                queryset = queryset.order_by(self.order.replace('name', 'i18n_name'))
            elif sort_key == 'group':
                queryset = queryset.order_by(self.order + '__name')
            elif sort_key == 'solved':
                if self.request.user.is_authenticated():
                    solved = user_completed_ids(self.request.user.profile)
                    queryset = list(queryset)
                    queryset.sort(key=lambda problem: problem.id in solved, reverse=self.order.startswith('-'))
            elif sort_key == 'type':
                if self.show_types:
                    queryset = list(queryset)
                    queryset.sort(key=lambda problem: problem.types_list[0] if problem.types_list else '',
                                  reverse=self.order.startswith('-'))
            paginator.object_list = queryset
        return paginator

    @cached_property
    def profile(self):
        if not self.request.user.is_authenticated():
            return None
        return self.request.user.profile

    @cached_property
    def in_contest(self):
        return self.profile is not None and self.profile.current_contest is not None

    def get_contest_queryset(self):
        queryset = self.profile.current_contest.contest.contest_problems.select_related('problem__group') \
            .defer('problem__description').order_by('problem__code') \
            .annotate(user_count=Count('submission__participation', distinct=True)) \
            .order_by('order')
        queryset = TranslatedProblemForeignKeyQuerySet.add_problem_i18n_name.im_func(queryset, 'i18n_name',
                                                                                     self.request.LANGUAGE_CODE,
                                                                                     'problem__name')
        return [{
                    'id': p['problem_id'],
                    'code': p['problem__code'],
                    'name': p['problem__name'],
                    'i18n_name': p['i18n_name'],
                    'group': {'full_name': p['problem__group__full_name']},
                    'points': p['points'],
                    'partial': p['partial'],
                    'user_count': p['user_count'],
                } for p in queryset.values('problem_id', 'problem__code', 'problem__name', 'i18n_name',
                                           'problem__group__full_name', 'points', 'partial', 'user_count')]

    def get_normal_queryset(self):
        filter = Q(is_public=True)
        if self.profile is not None:
            filter |= Q(id__in=Problem.objects.filter(contest__users__user=self.profile).values('id').distinct())
            filter |= Q(authors=self.profile)
        queryset = Problem.objects.filter(filter).select_related('group').defer('description')
        if self.profile is not None and self.hide_solved:
            queryset = queryset.exclude(id__in=Submission.objects.filter(user=self.profile, points=F('problem__points'))
                                        .values_list('problem__id', flat=True))
        if self.show_types:
            queryset = queryset.prefetch_related('types')
        if self.category is not None:
            queryset = queryset.filter(group__id=self.category)
        if self.selected_types:
            queryset = queryset.filter(types__in=self.selected_types)
        if 'search' in self.request.GET:
            self.search_query = query = ' '.join(self.request.GET.getlist('search')).strip()
            if query:
                if settings.ENABLE_FTS and self.full_text:
                    queryset = queryset.search(query, queryset.BOOLEAN).extra(order_by=['-relevance'])
                else:
                    queryset = queryset.filter(
                        Q(code__icontains=query) | Q(name__icontains=query) |
                        Q(translations__name__icontains=query, translations__language=self.request.LANGUAGE_CODE))
        return queryset.distinct()

    def get_queryset(self):
        if self.in_contest:
            return self.get_contest_queryset()
        else:
            return self.get_normal_queryset()

    def get_completed_problems(self):
        if self.in_contest:
            return contest_completed_ids(self.profile.current_contest)
        else:
            return user_completed_ids(self.profile) if self.profile is not None else ()

    def get_attempted_problems(self):
        if self.in_contest:
            return contest_attempted_ids(self.profile.current_contest)
        else:
            return user_attempted_ids(self.profile) if self.profile is not None else ()

    def get_context_data(self, **kwargs):
        context = super(ProblemList, self).get_context_data(**kwargs)
        context['hide_solved'] = 0 if self.in_contest else int(self.hide_solved)
        context['show_types'] = 0 if self.in_contest else int(self.show_types)
        context['full_text'] = 0 if self.in_contest else int(self.full_text)
        context['category'] = self.category
        context['categories'] = ProblemGroup.objects.all()
        if self.show_types:
            context['selected_types'] = self.selected_types
            context['problem_types'] = ProblemType.objects.all()
        context['has_fts'] = settings.ENABLE_FTS
        context['search_query'] = self.search_query
        context['completed_problem_ids'] = self.get_completed_problems()
        context['attempted_problems'] = self.get_attempted_problems()

        context.update(self.get_sort_paginate_context())
        if not self.in_contest:
            context.update(self.get_sort_context())
        return context

    def GET_with_session(self, request, key):
        if not request.GET:
            return request.session.get(key, False)
        return request.GET.get(key, None) == '1'

    def setup(self, request):
        self.hide_solved = self.GET_with_session(request, 'hide_solved')
        self.show_types = self.GET_with_session(request, 'show_types')
        self.full_text = self.GET_with_session(request, 'full_text')

        self.search_query = None
        self.category = None
        self.selected_types = []
        if 'category' in request.GET:
            try:
                self.category = int(request.GET.get('category'))
            except ValueError:
                pass
        if 'type' in request.GET:
            try:
                self.selected_types = map(int, request.GET.getlist('type'))
            except ValueError:
                pass

    def get(self, request, *args, **kwargs):
        self.setup(request)

        try:
            return super(ProblemList, self).get(request, *args, **kwargs)
        except ProgrammingError as e:
            return generic_message(request, 'FTS syntax error', e.args[1], status=400)

    def post(self, request, *args, **kwargs):
        to_update = ('hide_solved', 'show_types', 'full_text')
        for key in to_update:
            if key in request.GET:
                val = request.GET.get(key) == '1'
                request.session[key] = val
            else:
                request.session.pop(key, None)
        return HttpResponseRedirect(request.get_full_path())


class RandomProblem(ProblemList):
    def get(self, request, *args, **kwargs):
        self.setup(request)
        if self.in_contest:
            raise Http404()

        queryset = self.get_normal_queryset()
        count = queryset.count()
        if not count:
            return HttpResponseRedirect('%s%s%s' % (reverse('problem_list'), request.META['QUERY_STRING'] and '?',
                                                    request.META['QUERY_STRING']))
        return HttpResponseRedirect(queryset[randrange(count)].get_absolute_url())


user_logger = logging.getLogger('judge.user')


@login_required
def problem_submit(request, problem=None, submission=None):
    if submission is not None and not request.user.has_perm('judge.resubmit_other') and \
                    get_object_or_404(Submission, id=int(submission)).user.user != request.user:
        raise PermissionDenied()

    profile = request.user.profile
    if request.method == 'POST':
        form = ProblemSubmitForm(request.POST, instance=Submission(user=profile))
        if form.is_valid():
            if (not request.user.has_perm('judge.spam_submission') and
                        Submission.objects.filter(user=profile, was_rejudged=False).exclude(
                            status__in=['D', 'IE', 'CE', 'AB']).count() > 2):
                return HttpResponse('<h1>You submitted too many submissions.</h1>', status=503)
            if not form.cleaned_data['problem'].allowed_languages.filter(
                    id=form.cleaned_data['language'].id).exists():
                raise PermissionDenied()
            if not form.cleaned_data['problem'].is_accessible_by(request.user):
                user_logger.info('Naughty user %s wants to submit to %s without permission',
                                 request.user.username, form.cleaned_data['problem'].code)
                return HttpResponseForbidden('<h1>Do you want me to ban you?</h1>')
            if not request.user.is_superuser and form.cleaned_data['problem'].banned_users.filter(
                    id=profile.id).exists():
                return generic_message(request, _('Banned from Submitting'),
                                       _('You have been declared persona non grata for this problem. '
                                         'You are permanently barred from submitting this problem.'))
            model = form.save()

            profile.update_contest()
            if profile.current_contest is not None:
                try:
                    contest_problem = model.problem.contests.get(contest=profile.current_contest.contest)
                except ContestProblem.DoesNotExist:
                    pass
                else:
                    contest = ContestSubmission(submission=model, problem=contest_problem,
                                                participation=profile.current_contest)
                    contest.save()

            model.judge()
            return HttpResponseRedirect(reverse('submission_status', args=[str(model.id)]))
        else:
            form_data = form.cleaned_data
    else:
        initial = {'language': profile.language}
        if problem is not None:
            initial['problem'] = get_object_or_404(Problem, code=problem)
            if not initial['problem'].is_accessible_by(request.user):
                raise Http404()
        if submission is not None:
            try:
                sub = get_object_or_404(Submission, id=int(submission))
                initial['source'] = sub.source
                initial['language'] = sub.language
            except ValueError:
                raise Http404()
        form = ProblemSubmitForm(initial=initial)
        form_data = initial
    if 'problem' in form_data:
        form.fields['language'].queryset = (form_data['problem'].usable_languages.order_by('name', 'key')
                                            .prefetch_related(
            Prefetch('runtimeversion_set', RuntimeVersion.objects.order_by('priority'))))
    if 'language' in form_data:
        form.fields['source'].widget.mode = form_data['language'].ace
    form.fields['source'].widget.theme = profile.ace_theme
    return render(request, 'problem/submit.jade', {
        'form': form,
        'title': _('Submit'),
        'langs': Language.objects.all(),
        'no_judges': not form.fields['language'].queryset,
        'ACE_URL': ACE_URL
    })


@require_POST
@login_required
@permission_required('judge.clone_problem')
def clone_problem(request, problem):
    problem = get_object_or_404(Problem, code=problem)
    languages = problem.allowed_languages.all()
    types = problem.types.all()
    problem.pk = None
    problem.is_public = False
    problem.code += '_clone'
    try:
        problem.save()
    except IntegrityError:
        code = problem.code
        for i in itertools.count(1):
            problem.code = '%s%d' % (code, i)
            try:
                problem.save()
            except IntegrityError:
                pass
            else:
                break
    problem.authors.add(request.user.profile)
    problem.allowed_languages = languages
    problem.types = types
    return HttpResponseRedirect(reverse('admin:judge_problem_change', args=(problem.id,)))
