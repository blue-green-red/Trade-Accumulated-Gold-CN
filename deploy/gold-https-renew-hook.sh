#!/bin/sh
set -eu

# Certbot can manage other sites too: reload only for the dashboard certificate.
case " ${RENEWED_DOMAINS:-} " in
    *" gold.goalgoal.fun "*)
        /usr/sbin/nginx -t -c /etc/nginx/gold.conf
        /bin/systemctl reload gold-https.service
        ;;
esac
