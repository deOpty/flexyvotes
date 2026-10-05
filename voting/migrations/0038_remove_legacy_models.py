from django.db import migrations


class Migration(migrations.Migration):
    """Drop legacy structures only after 0037 has copied their data into the
    new models (ActivityLog -> core.AuditEvent, VotingCode -> elections.Voter,
    is_approved/voting_locked -> Event.status)."""

    dependencies = [
        ("voting", "0037_migrate_legacy_data"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="activitylog",
            name="event",
        ),
        migrations.RemoveField(
            model_name="activitylog",
            name="user",
        ),
        migrations.AlterUniqueTogether(
            name="votingcode",
            unique_together=None,
        ),
        migrations.RemoveField(
            model_name="votingcode",
            name="event",
        ),
        migrations.RemoveField(
            model_name="event",
            name="is_approved",
        ),
        migrations.RemoveField(
            model_name="event",
            name="voting_locked",
        ),
        migrations.DeleteModel(
            name="ActivityLog",
        ),
        migrations.DeleteModel(
            name="VotingCode",
        ),
    ]
