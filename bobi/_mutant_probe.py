def platform_name():
    # A container without either signal: plain `docker run` under an
    # unknown runtime. See docker-entrypoint.sh for the root choice.
    return "docker"
