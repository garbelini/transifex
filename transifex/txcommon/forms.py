from django import forms
from contact_form.forms import ContactForm
from userena.forms import EditProfileForm as UserenaEditProfileForm
from userena.utils import get_profile_model
from tagging.forms import TagField
from tagging_autocomplete.widgets import TagAutocomplete


class EditProfileForm(UserenaEditProfileForm):

    def __init__(self, *args, **kw):
        super(forms.ModelForm, self).__init__(*args, **kw)

    def clean_tags(self):
        user_tags_list = self.cleaned_data['tags']
        tags = list(set([tag.strip() for tag in user_tags_list.split(',')])) or []
        for i in tags:
            if not i.strip():
                tags.remove(i)
        tags.append(u'')
        user_tags_list = ', '.join(tags)
        return user_tags_list

    class Meta:
        model = get_profile_model()
        exclude = ('user', 'privacy', 'mugshot', )
        fields = (
            'first_name', 'last_name', 'location', 'languages', 'tags', 'blog',
            'linked_in', 'twitter', 'about', 'looking_for_work'
        )
