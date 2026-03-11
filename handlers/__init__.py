"""Handler modules for aIRCp daemon HTTP routes.

Each module exports GET_ROUTES, POST_ROUTES, and optionally
GET_PREFIX_ROUTES and POST_RAW_ROUTES.
collect_routes() merges them with duplicate detection.
"""


def collect_routes():
    """Merge route tables from all handler modules.

    Returns (get_routes, post_routes, get_prefix_routes, post_raw_routes).
    Raises ValueError on duplicate route paths.
    """
    from . import system, projects, tasks, reviews, brainstorm
    from . import autonomy, messaging, workflow, extras, uploads
    from . import github

    modules = [
        system, projects, tasks, reviews, brainstorm,
        autonomy, messaging, workflow, extras, uploads,
        github,
    ]

    get_routes = {}
    post_routes = {}
    get_prefix_routes = []
    post_raw_routes = {}

    for mod in modules:
        mod_name = mod.__name__

        for path, fn in getattr(mod, "GET_ROUTES", {}).items():
            if path in get_routes:
                raise ValueError(
                    f"Duplicate GET route '{path}': {mod_name} vs "
                    f"{get_routes[path].__module__}"
                )
            get_routes[path] = fn

        for path, fn in getattr(mod, "POST_ROUTES", {}).items():
            if path in post_routes:
                raise ValueError(
                    f"Duplicate POST route '{path}': {mod_name} vs "
                    f"{post_routes[path].__module__}"
                )
            post_routes[path] = fn

        for prefix, fn in getattr(mod, "GET_PREFIX_ROUTES", []):
            get_prefix_routes.append((prefix, fn))

        for path, fn in getattr(mod, "POST_RAW_ROUTES", {}).items():
            if path in post_raw_routes:
                raise ValueError(
                    f"Duplicate POST_RAW route '{path}': {mod_name} vs "
                    f"{post_raw_routes[path].__module__}"
                )
            post_raw_routes[path] = fn

    return get_routes, post_routes, get_prefix_routes, post_raw_routes
