"""Launch only a forward-media lane with an explicit, validated clean environment.

The operator supplies a complete mapping from dedicated secret configuration.
Never copy os.environ or silently select credentials out of the publisher process.
"""
import subprocess
import sys

from .forward_media_lane import unknown_environment_names


class LaneLaunchError(RuntimeError):
    pass


def launch_isolated(lane, arguments, *, environment):
    """Run the fixed lane entrypoint; environment is required and never inherited."""
    if lane not in ('owner', 'attester'):
        raise LaneLaunchError('unknown isolated lane')
    if unknown_environment_names(environment, lane):
        raise LaneLaunchError('unrecognized isolated environment')
    try:
        if lane == 'owner':
            from .forward_media_owner import check_environment
            check_environment(environment)
            module = 'agent.forward_media_owner_packet'
        else:
            from .forward_media_attester_worker import settings_from_environment
            settings_from_environment(environment)
            module = 'agent.forward_media_attester_worker'
    except Exception:
        raise LaneLaunchError('dedicated isolated configuration required') from None
    return subprocess.run([sys.executable, '-m', module, *arguments],
                          env=dict(environment), check=False)
