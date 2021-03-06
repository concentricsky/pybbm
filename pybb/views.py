# -*- coding: utf-8 -*-

import math

from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.core.urlresolvers import reverse
from django.contrib import messages
from django.db import transaction
from django.db.models import F, Q
from django.http import HttpResponseRedirect, HttpResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, _get_queryset
from django.utils.translation import ugettext_lazy as _
from django.utils.decorators import method_decorator
from django.views.generic.edit import ModelFormMixin
from django.views.decorators.csrf import csrf_protect

try:
    from django.views import generic
except ImportError:
    try:
        from cbv import generic
    except ImportError:
        raise ImportError('If you using django version < 1.3 you should install django-cbv for pybb')

from pure_pagination import Paginator

from pybb.models import Category, Forum, Topic, Post, TopicReadTracker, ForumReadTracker, PollAnswerUser
from pybb.forms import  PostForm, AdminPostForm, EditProfileForm, AttachmentFormSet, PollAnswerFormSet, PollForm
from pybb.templatetags.pybb_tags import pybb_editable_by, pybb_topic_poll_not_voted
from pybb.templatetags.pybb_tags import pybb_topic_moderated_by
from pybb import defaults


def filter_hidden(request, queryset_or_model):
    """
    Return queryset for model, manager or queryset, filtering hidden objects for non staff users.
    """
    queryset = _get_queryset(queryset_or_model)
    if request.user.is_staff:
        return queryset
    return queryset.filter(hidden=False)

class IndexView(generic.ListView):

    template_name = 'pybb/index.html'
    context_object_name = 'categories'

    def get_context_data(self, **kwargs):
        ctx = super(IndexView, self).get_context_data(**kwargs)
        categories = list(ctx['categories'])
        for category in categories:
            category.forums_accessed = filter_hidden(self.request, category.forums.all())
        ctx['categories'] = categories
        return ctx

    def get_queryset(self):
        return filter_hidden(self.request, Category)

    
class CategoryView(generic.DetailView):

    template_name = 'pybb/index.html'
    context_object_name = 'category'

    def get_queryset(self):
        return filter_hidden(self.request, Category)

    def get_context_data(self, **kwargs):
        ctx = super(CategoryView, self).get_context_data(**kwargs)
        ctx['category'].forums_accessed = filter_hidden(self.request, ctx['category'].forums.all())
        ctx['categories'] = [ctx['category']]
        return ctx


class ForumView(generic.ListView):

    paginate_by = defaults.PYBB_FORUM_PAGE_SIZE
    context_object_name = 'topic_list'
    template_name = 'pybb/forum.html'
    paginator_class = Paginator

    def get_context_data(self, **kwargs):
        ctx = super(ForumView, self).get_context_data(**kwargs)
        ctx['forum'] = self.forum
        return ctx

    def get_queryset(self):
        self.forum = get_object_or_404(filter_hidden(self.request, Forum), pk=self.kwargs['pk'])
        if self.forum.category.hidden and (not self.request.user.is_staff):
            raise Http404()
        qs = self.forum.topics.order_by('-sticky', '-updated').select_related()
        if not (self.request.user.is_superuser or self.request.user in self.forum.moderators.all()):
            if self.request.user.is_authenticated():
                qs = qs.filter(Q(user=self.request.user)|Q(on_moderation=False))
            else:
                qs = qs.filter(on_moderation=False)
        return qs
    

class TopicView(generic.ListView):
    paginate_by = defaults.PYBB_TOPIC_PAGE_SIZE
    template_object_name = 'post_list'
    template_name = 'pybb/topic.html'
    paginator_class = Paginator

    def get_queryset(self):
        self.topic = get_object_or_404(Topic.objects.select_related('forum'), pk=self.kwargs['pk'])
        if self.topic.on_moderation and\
           not pybb_topic_moderated_by(self.topic, self.request.user) and\
           not self.request.user == self.topic.user:
            raise PermissionDenied
        if (self.topic.forum.hidden or self.topic.forum.category.hidden) and (not self.request.user.is_staff):
            raise Http404()
        self.topic.views += 1
        self.topic.save()
        qs = self.topic.posts.all().select_related('user')
        if not pybb_topic_moderated_by(self.topic, self.request.user):
            if self.request.user.is_authenticated():
                qs = qs.filter(Q(user=self.request.user)|Q(on_moderation=False))
            else:
                qs = qs.filter(on_moderation=False)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super(TopicView, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated():
            self.request.user.is_moderator = self.request.user.is_superuser or (self.request.user in self.topic.forum.moderators.all())
            self.request.user.is_subscribed = self.request.user in self.topic.subscribers.all()
            if self.request.user.is_staff:
                ctx['form'] = AdminPostForm(initial={'login': self.request.user.username}, topic=self.topic)
            else:
                ctx['form'] = PostForm(topic=self.topic)
            self.mark_read(self.request, self.topic)
        elif defaults.PYBB_ENABLE_ANONYMOUS_POST:
            ctx['form'] = PostForm(topic=self.topic)
        else:
            ctx['form'] = None
        if defaults.PYBB_ATTACHMENT_ENABLE:
            aformset = AttachmentFormSet()
            ctx['aformset'] = aformset
        if defaults.PYBB_FREEZE_FIRST_POST:
            ctx['first_post'] = self.topic.head
        else:
            ctx['first_post'] = None
        ctx['topic'] = self.topic

        if self.request.user.is_authenticated() and self.topic.poll_type != Topic.POLL_TYPE_NONE and \
           pybb_topic_poll_not_voted(self.topic, self.request.user):
            ctx['poll_form'] = PollForm(self.topic)

        return ctx

    def mark_read(self, request, topic):
        try:
            forum_mark = ForumReadTracker.objects.get(forum=topic.forum, user=request.user)
        except ObjectDoesNotExist:
            forum_mark = None
        if (forum_mark is None) or (forum_mark.time_stamp < topic.updated):
            # Mark topic as readed
            topic_mark, new = TopicReadTracker.objects.get_or_create(topic=topic, user=request.user)
            if not new:
                topic_mark.save()

            # Check, if there are any unread topics in forum
            read = Topic.objects.filter(Q(forum=topic.forum) & (Q(topicreadtracker__user=request.user,topicreadtracker__time_stamp__gt=F('updated'))) | 
                                                                Q(forum__forumreadtracker__user=request.user,forum__forumreadtracker__time_stamp__gt=F('updated')))
            unread = Topic.objects.filter(forum=topic.forum).exclude(id__in=read)
            if not unread.exists():
                # Clear all topic marks for this forum, mark forum as readed
                TopicReadTracker.objects.filter(
                        user=request.user,
                        topic__forum=topic.forum
                        ).delete()
                forum_mark, new = ForumReadTracker.objects.get_or_create(forum=topic.forum, user=request.user)
                forum_mark.save()


class PostEditMixin(object):

    def get_form_class(self):
        if self.request.user.is_staff:
            return AdminPostForm
        else:
            return PostForm

    def get_context_data(self, **kwargs):
        ctx = super(PostEditMixin, self).get_context_data(**kwargs)
        if defaults.PYBB_ATTACHMENT_ENABLE and (not 'aformset' in kwargs):
            ctx['aformset'] = AttachmentFormSet(instance=self.object if getattr(self, 'object') else None)
        if 'pollformset' not in kwargs:
            ctx['pollformset'] = PollAnswerFormSet(instance=self.object.topic if getattr(self, 'object') else None)
        return ctx

    def form_valid(self, form):
        success = True
        with transaction.commit_manually():
            self.object = form.save()

            if defaults.PYBB_ATTACHMENT_ENABLE:
                aformset = AttachmentFormSet(self.request.POST, self.request.FILES, instance=self.object)
                if aformset.is_valid():
                    aformset.save()
                else:
                    success = False
            else:
                aformset = AttachmentFormSet()

            if self.object.topic.poll_type != Topic.POLL_TYPE_NONE and self.object.topic.head == self.object:
                pollformset = PollAnswerFormSet(self.request.POST, instance=self.object.topic)
                if pollformset.is_valid():
                    pollformset.save()
                else:
                    success = False
            else:
                self.object.topic.poll_question = None
                self.object.topic.save()
                self.object.topic.poll_answers.all().delete()
                pollformset = PollAnswerFormSet()

            if success:
                transaction.commit()
                return super(ModelFormMixin, self).form_valid(form)
            else:
                transaction.rollback()
                return self.render_to_response(self.get_context_data(form=form, aformset=aformset, pollformset=pollformset))


class AddPostView(PostEditMixin, generic.CreateView):

    template_name = 'pybb/add_post.html'

    def get_form_kwargs(self):
        ip = self.request.META.get('REMOTE_ADDR', '')
        form_kwargs = super(AddPostView, self).get_form_kwargs()
        form_kwargs.update(dict(topic=self.topic, forum=self.forum, user=self.user,
                       ip=ip, initial={}))
        if 'quote_id' in self.request.GET:
            try:
                quote_id = int(self.request.GET.get('quote_id'))
            except TypeError:
                raise Http404
            else:
                post = get_object_or_404(Post, pk=quote_id)
                quote = defaults.PYBB_QUOTE_ENGINES[defaults.PYBB_MARKUP](post.body, post.user.username)
                form_kwargs['initial']['body'] = quote
        if self.user.is_staff:
            form_kwargs['initial']['login'] = self.user.username
        return form_kwargs

    def get_context_data(self, **kwargs):
        ctx = super(AddPostView, self).get_context_data(**kwargs)
        ctx['forum'] = self.forum
        ctx['topic'] = self.topic
        return ctx

    def get_success_url(self):
        if (not self.request.user.is_authenticated()) and defaults.PYBB_PREMODERATION:
            return reverse('pybb:index')
        return super(AddPostView, self).get_success_url()

    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated():
            self.user = request.user
        else:
            if defaults.PYBB_ENABLE_ANONYMOUS_POST:
                self.user, new = User.objects.get_or_create(username=defaults.PYBB_ANONYMOUS_USERNAME)
            else:
                from django.contrib.auth.views import redirect_to_login
                return redirect_to_login(request.get_full_path())

        if not self.user.has_perm('pybb.add_post'):
            raise PermissionDenied

        self.forum = None
        self.topic = None
        if 'forum_id' in kwargs:
            self.forum = get_object_or_404(filter_hidden(request, Forum), pk=kwargs['forum_id'])
        elif 'topic_id' in kwargs:
            self.topic = get_object_or_404(Topic, pk=kwargs['topic_id'])
            if self.topic.forum.hidden and (not self.user.is_staff):
                raise Http404
            if self.topic.closed:
                raise PermissionDenied
        return super(AddPostView, self).dispatch(request, *args, **kwargs)


class EditPostView(PostEditMixin, generic.UpdateView):

    model = Post

    context_object_name = 'post'
    template_name = 'pybb/edit_post.html'

    @method_decorator(login_required)
    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super(EditPostView, self).dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        post = super(EditPostView, self).get_object(queryset)
        if not pybb_editable_by(post, self.request.user):
            raise PermissionDenied
        return post


class UserView(generic.DetailView):
    model = User
    template_name = 'pybb/user.html'
    context_object_name = 'target_user'

    def get_object(self, queryset=None):
        if queryset is None:
            queryset = self.get_queryset()
        return get_object_or_404(queryset, username=self.kwargs['username'])

    def get_context_data(self, **kwargs):
        ctx = super(UserView, self).get_context_data(**kwargs)
        ctx['topic_count'] = Topic.objects.filter(user=ctx['target_user']).count()
        return ctx
        

class PostView(generic.RedirectView):
    def get_redirect_url(self, **kwargs):
        post = get_object_or_404(Post, pk=self.kwargs['pk'])
        if defaults.PYBB_PREMODERATION and\
            post.on_moderation and\
            (not pybb_topic_moderated_by(post.topic, self.request.user)) and\
            (not post.user==self.request.user):
            raise PermissionDenied
        count = post.topic.posts.filter(created__lt=post.created).count() + 1
        page = math.ceil(count / float(defaults.PYBB_TOPIC_PAGE_SIZE))
        return '%s?page=%d#post-%d' % (reverse('pybb:topic', args=[post.topic.id]), page, post.id)


class ModeratePost(generic.RedirectView):
    def get_redirect_url(self, **kwargs):
        post = get_object_or_404(Post, pk=self.kwargs['pk'])
        if not pybb_topic_moderated_by(post.topic, self.request.user):
            raise PermissionDenied
        post.on_moderation = False
        post.save()
        return post.get_absolute_url()


class ProfileEditView(generic.UpdateView):

    template_name = 'pybb/edit_profile.html'
    form_class = EditProfileForm

    def get_object(self, queryset=None):
        return self.request.user.profile

    @method_decorator(login_required)
    @method_decorator(csrf_protect)
    def dispatch(self, request, *args, **kwargs):
        return super(ProfileEditView, self).dispatch(request, *args, **kwargs)

    def get_success_url(self):
        return reverse('pybb:edit_profile')


class DeletePostView(generic.DeleteView):

    template_name = 'pybb/delete_post.html'
    context_object_name = 'post'

    def get_object(self, queryset=None):
        post = get_object_or_404(Post.objects.select_related('topic', 'topic__forum'), pk=self.kwargs['pk'])
        self.topic = post.topic
        self.forum = post.topic.forum
        if not pybb_topic_moderated_by(self.topic, self.request.user):
           raise PermissionDenied
        return post

    def get_success_url(self):
        try:
            Topic.objects.get(pk=self.topic.id)
        except Topic.DoesNotExist:
            return self.forum.get_absolute_url()
        else:
            return self.topic.get_absolute_url()


class TopicActionBaseView(generic.View):

    def get_topic(self):
        topic = get_object_or_404(Topic, pk=self.kwargs['pk'])
        if not pybb_topic_moderated_by(topic, self.request.user):
            raise PermissionDenied
        return topic

    @method_decorator(login_required)
    def get(self, *args, **kwargs):
        self.topic = self.get_topic()
        self.action(self.topic)
        return HttpResponseRedirect(self.topic.get_absolute_url())


class StickTopicView(TopicActionBaseView):
    def action(self, topic):
        topic.sticky = True
        topic.save()


class UnstickTopicView(TopicActionBaseView):
    def action(self, topic):
        topic.sticky = False
        topic.save()


class CloseTopicView(TopicActionBaseView):
    def action(self, topic):
        topic.closed = True
        topic.save()

class OpenTopicView(TopicActionBaseView):
    def action(self, topic):
        topic.closed = False
        topic.save()

class TopicPollVoteView(generic.UpdateView):
    model = Topic
    http_method_names = ['post', ]
    form_class = PollForm

    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs):
        return super(TopicPollVoteView, self).dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super(ModelFormMixin, self).get_form_kwargs()
        kwargs['topic'] = self.object
        return kwargs

    def form_valid(self, form):
        # already voted
        if not pybb_topic_poll_not_voted(self.object, self.request.user):
            return HttpResponseBadRequest()

        answers = form.cleaned_data['answers']
        for answer in answers:
            # poll answer from another topic
            if answer.topic != self.object:
                return HttpResponseBadRequest()

            PollAnswerUser.objects.create(poll_answer=answer, user=self.request.user)
        return super(ModelFormMixin, self).form_valid(form)

    def form_invalid(self, form):
        return self.object.get_absolute_url()

    def get_success_url(self):
        return self.object.get_absolute_url()


@login_required
def delete_subscription(request, topic_id):
    topic = get_object_or_404(Topic, pk=topic_id)
    topic.subscribers.remove(request.user)
    return HttpResponseRedirect(topic.get_absolute_url())

@login_required
def add_subscription(request, topic_id):
    topic = get_object_or_404(Topic, pk=topic_id)
    topic.subscribers.add(request.user)
    return HttpResponseRedirect(topic.get_absolute_url())

@login_required
def post_ajax_preview(request):
    content = request.POST.get('data')
    html = defaults.PYBB_MARKUP_ENGINES[defaults.PYBB_MARKUP](content)
    return HttpResponse(html)

@login_required
def mark_all_as_read(request):
    for forum in filter_hidden(request, Forum):
        forum_mark, new = ForumReadTracker.objects.get_or_create(forum=forum, user=request.user)
        forum_mark.save()
    TopicReadTracker.objects.filter(user=request.user).delete()
    msg = _('All forums marked as read')
    messages.success(request, msg, fail_silently=True)
    return redirect(reverse('pybb:index'))

@login_required
@permission_required('pybb.block_users')
def block_user(request, username):
    user = get_object_or_404(User, username=username)
    user.is_active = False
    user.save()
    msg = _('User successfuly blocked')
    messages.success(request, msg, fail_silently=True)
    return redirect('pybb:index')