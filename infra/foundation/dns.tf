locals {
  app_fqdn = "${var.app_hostname}.${var.domain_name}"
}

# The hosted zone for the whole domain. Once the domain's nameservers are set to this zone's (output
# `name_servers`), Route53 answers for it. Until then nothing public resolves through it.
# (Hostinger's DNS editor has no NS record type, so a subdomain-only delegation was not possible while the
# domain is still being transferred; the apex moves here when the transfer completes.)
resource "aws_route53_zone" "main" {
  name    = var.domain_name
  comment = "PR review agent"
}

# Email records copied from the domain's Hostinger DNS (read from public DNS on 2026-11-12). The domain has live
# Hostinger mail, so moving the nameservers to this zone without these would stop mail. Mail itself stays at Hostinger.
# The parking-page A record and the www CNAME are deliberately not copied.
resource "aws_route53_record" "mail_mx" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "MX"
  ttl     = 3600
  records = ["5 mx1.hostinger.com", "10 mx2.hostinger.com"]
}

resource "aws_route53_record" "mail_spf" {
  zone_id = aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "TXT"
  ttl     = 3600
  records = ["v=spf1 include:_spf.mail.hostinger.com ~all"]
}

resource "aws_route53_record" "mail_dmarc" {
  zone_id = aws_route53_zone.main.zone_id
  name    = "_dmarc.${var.domain_name}"
  type    = "TXT"
  ttl     = 3600
  records = ["v=DMARC1; p=none"]
}

resource "aws_route53_record" "mail_cname" {
  for_each = {
    "hostingermail-a._domainkey" = "hostingermail-a.dkim.mail.hostinger.com"
    "hostingermail-b._domainkey" = "hostingermail-b.dkim.mail.hostinger.com"
    "hostingermail-c._domainkey" = "hostingermail-c.dkim.mail.hostinger.com"
    "autodiscover"               = "autodiscover.mail.hostinger.com"
    "autoconfig"                 = "autoconfig.mail.hostinger.com"
  }

  zone_id = aws_route53_zone.main.zone_id
  name    = "${each.key}.${var.domain_name}"
  type    = "CNAME"
  ttl     = 3600
  records = [each.value]
}

# Certificate for the webhook hostname. DNS-validated: the validation CNAMEs are created in the zone above.
# Off by default: validation only completes after the nameservers point here.
resource "aws_acm_certificate" "app" {
  count = var.enable_certificate ? 1 : 0

  domain_name       = local.app_fqdn
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = var.enable_certificate ? {
    for o in aws_acm_certificate.app[0].domain_validation_options : o.domain_name => {
      name   = o.resource_record_name
      type   = o.resource_record_type
      record = o.resource_record_value
    }
  } : {}

  zone_id         = aws_route53_zone.main.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "app" {
  count = var.enable_certificate ? 1 : 0

  certificate_arn         = aws_acm_certificate.app[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}
