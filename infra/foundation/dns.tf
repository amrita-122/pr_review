locals {
  app_fqdn = "${var.app_hostname}.${var.domain_name}"
}

# The hosted zone for the whole domain. Once the domain's nameservers are set to this zone's (output
# `name_servers`), Route53 answers for it. Until then nothing public resolves through it.
resource "aws_route53_zone" "main" {
  name    = var.domain_name
  comment = "PR review agent"
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
