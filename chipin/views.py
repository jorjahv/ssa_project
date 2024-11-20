from django.http import HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.mail import send_mail
from django.urls import reverse
from django.conf import settings
from django.contrib.auth.models import User
from .forms import GroupCreationForm, CommentForm
from .models import Group, GroupJoinRequest, Comment, Event
import urllib.parse


@login_required
def home(request):
    user = request.user
    pending_invitations = user.pending_invitations.all()  # Get pending group invitations for the current user
    user_groups = user.group_memberships.all()  # Get groups the user is a member of
    user_join_requests = GroupJoinRequest.objects.filter(user=user)  # Get join requests sent by the user
    available_groups = Group.objects.exclude(members=user).exclude(join_requests__user=user)  # Get groups the user is not a member of and the user has not requested to join
    context = {
        'pending_invitations': pending_invitations,
        'user_groups': user_groups,
        'user_join_requests': user_join_requests,
        'available_groups': available_groups
    }
    return render(request, 'chipin/home.html', context)


@login_required
def group_detail(request, group_id, edit_comment_id=None):
    group = get_object_or_404(Group, id=group_id)
    comments = group.comments.all().order_by('-created_at')  # Fetch all comments for the group
    events = group.events.all()  # Fetch all events associated with the group

    # Add a new comment or edit an existing comment
    if edit_comment_id:  # Fetch the comment to edit, if edit_comment_id is provided
        comment_to_edit = get_object_or_404(Comment, id=edit_comment_id)
        if comment_to_edit.user != request.user:
            return redirect('chipin:group_detail', group_id=group.id)
    else:
        comment_to_edit = None

    if request.method == 'POST':
        if comment_to_edit:  # Editing an existing comment
            form = CommentForm(request.POST, instance=comment_to_edit)
        else:  # Adding a new comment
            form = CommentForm(request.POST)
        if form.is_valid():
            comment = form.save(commit=False)
            comment.user = request.user
            comment.group = group
            comment.save()
            return redirect('chipin:group_detail', group_id=group.id)
    else:
        form = CommentForm(instance=comment_to_edit) if comment_to_edit else CommentForm()

    # Calculate event share for each event and check user eligibility
    event_share_info = {}
    for event in events:
        event_share = event.calculate_share()
        user_eligible = request.user.profile.max_spend >= event_share
        user_has_joined = request.user in event.members.all()  # Check if the user has already joined the event
        event_share_info[event] = {
            'share': event_share,
            'eligible': user_eligible,
            'status': event.status,
            'joined': user_has_joined
        }

    # Return data to the template
    return render(request, 'chipin/group_detail.html', {
        'group': group,
        'comments': comments,
        'form': form,
        'comment_to_edit': comment_to_edit,
        'events': events,
        'event_share_info': event_share_info,
    })


@login_required
def create_event(request, group_id):
    group = get_object_or_404(Group, id=group_id)
    if request.user != group.admin:
        messages.error(request, "Only the group administrator can create events.")
        return redirect('chipin:group_detail', group_id=group.id)
    if request.method == 'POST':
        event_name = request.POST.get('name')
        event_date = request.POST.get('date')
        total_spend = request.POST.get('total_spend')
        event = Event.objects.create(
            name=event_name,
            date=event_date,
            total_spend=total_spend,
            group=group
        )
        messages.success(request, f'Event "{event_name}" created successfully!')
        return redirect('chipin:group_detail', group_id=group.id)
    return render(request, 'chipin/create_event.html', {'group': group})


@login_required
def join_event(request, group_id, event_id):
    group = get_object_or_404(Group, id=group_id)
    event = get_object_or_404(Event, id=event_id, group=group)
    event_share = event.calculate_share()
    
    # Check if the user is eligible to join based on their max spend
    if request.user.profile.max_spend < event_share:
        messages.error(request, f"Your max spend of ${request.user.profile.max_spend} is too low to join this event.")
        return redirect('chipin:group_detail', group_id=group.id)
    # Check if the user has already joined the event
    if request.user in event.members.all():
        messages.info(request, "You have already joined this event.")
        return redirect('chipin:group_detail', group_id=group.id)
    
    # Add the user to the event
    event.members.add(request.user)
    messages.success(request, f"You have successfully joined the event '{event.name}'.")
    
    # Optionally, update the event status if needed
    event.check_status()
    event.save()
    return redirect('chipin:group_detail', group_id=group.id)


@login_required
def update_event_status(request, group_id, event_id):
    group = get_object_or_404(Group, id=group_id)
    event = get_object_or_404(Event, id=event_id, group=group)

    if request.user != group.admin:
        messages.error(request, "Only the group administrator can update the event status.")
        return redirect('chipin:group_detail', group_id=group.id)

    event_share = event.calculate_share()

    sufficient_funds = all(
        member.profile.max_spend >= event_share for member in group.members.all()
    )

    if sufficient_funds:
        event.status = "Active"
        messages.success(request, f"The event '{event.name}' is now Active. All members can cover the cost.")
    else:
        event.status = "Pending"
        messages.warning(request, f"The event '{event.name}' remains Pending. Some members cannot cover the cost.")

    event.save()
    return redirect('chipin:group_detail', group_id=group.id)


@login_required
def leave_event(request, group_id, event_id):
    group = get_object_or_404(Group, id=group_id)
    event = get_object_or_404(Event, id=event_id, group=group)

    if request.user not in event.members.all():
        messages.error(request, "You are not a member of this event.")
        return redirect('chipin:group_detail', group_id=group.id)

    event.members.remove(request.user)
    messages.success(request, f"You have successfully left the event '{event.name}'.")
    event.check_status()
    event.save()
    return redirect('chipin:group_detail', group_id=group.id)


@login_required
def delete_event(request, group_id, event_id):
    group = get_object_or_404(Group, id=group_id)
    event = get_object_or_404(Event, id=event_id, group=group)

    if request.user != group.admin:
        messages.error(request, "Only the group administrator can delete events.")
        return redirect('chipin:group_detail', group_id=group.id)

    event.delete()
    messages.success(request, f"The event '{event.name}' has been deleted.")
    return redirect('chipin:group_detail', group_id=group.id)
