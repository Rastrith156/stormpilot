from app import security


def create_authenticated_user(role='ADMIN', username=None):
    username = username or f'test-{role.lower()}'
    password = 'test-only-password-with-at-least-24-chars'
    status, user = security.create_user(username, password, role)
    if status != 201:
        raise AssertionError(user)
    status, _, response_headers = security.login({'username': username, 'password': password})
    if status != 200:
        raise AssertionError('Test account login failed.')
    cookies = {}
    for name, value in response_headers:
        if name.lower() == 'set-cookie':
            key, _, remainder = value.partition('=')
            cookies[key] = remainder.split(';', 1)[0]
    csrf = cookies['stormpilot_csrf']
    return {
        'Cookie': f"stormpilot_session={cookies['stormpilot_session']}; stormpilot_csrf={csrf}",
        'X-CSRF-Token': csrf,
    }
