package mycelium

import (
	"errors"
	"net/url"
	"regexp"
	"strconv"
	"strings"
)

var decimalPattern = regexp.MustCompile(`^(?:(?:0|-?[1-9][0-9]*)(\.[0-9]*[1-9])?|-0\.[0-9]*[1-9])$`)

func Decimal(value string) (DecimalValue, error) {
	if !decimalPattern.MatchString(value) || value == "-0" || strings.HasSuffix(value, "0") && strings.Contains(value, ".") {
		return DecimalValue{}, errors.New("decimal is not in decimal-1 form")
	}
	digits := strings.NewReplacer("-", "", ".", "").Replace(value)
	scale := 0
	if point := strings.IndexByte(value, '.'); point >= 0 {
		scale = len(value) - point - 1
	}
	if len(digits) > 38 || scale > 18 {
		return DecimalValue{}, errors.New("decimal exceeds decimal-1 limits")
	}
	return DecimalValue{Type: "decimal", Profile: "decimal-1", Value: value}, nil
}

func URL(value string) (URLValue, error) {
	if value == "" {
		return URLValue{}, errors.New("URL is empty")
	}
	for _, r := range value {
		if r < 0x20 || r == 0x7f {
			return URLValue{}, errors.New("URL contains controls")
		}
	}
	schemeEnd := strings.IndexByte(value, ':')
	if schemeEnd <= 0 || value[:schemeEnd] != strings.ToLower(value[:schemeEnd]) {
		return URLValue{}, errors.New("URL scheme must be lowercase")
	}
	parsed, err := url.Parse(value)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") || parsed.Host == "" || parsed.User != nil || parsed.Fragment != "" {
		return URLValue{}, errors.New("URL is not in url-1 form")
	}
	authority := value[schemeEnd+3:]
	if end := strings.IndexAny(authority, "/?#"); end >= 0 {
		authority = authority[:end]
	}
	if at := strings.LastIndexByte(authority, '@'); at >= 0 {
		authority = authority[at+1:]
	}
	host := authority
	if strings.HasPrefix(host, "[") {
		if end := strings.IndexByte(host, ']'); end >= 0 {
			host = host[1:end]
		}
	} else if colon := strings.LastIndexByte(host, ':'); colon >= 0 {
		host = host[:colon]
	}
	if host != strings.ToLower(host) {
		return URLValue{}, errors.New("URL host must be lowercase")
	}
	port := ""
	if strings.HasPrefix(authority, "[") {
		if end := strings.IndexByte(authority, ']'); end >= 0 && len(authority) > end+1 {
			port = authority[end+1:]
		}
	} else if colon := strings.LastIndexByte(authority, ':'); colon >= 0 {
		port = authority[colon:]
	}
	if port != "" {
		if !strings.HasPrefix(port, ":") {
			return URLValue{}, errors.New("URL port is invalid")
		}
		n, err := strconv.Atoi(port[1:])
		if err != nil || n < 1 || n > 65535 {
			return URLValue{}, errors.New("URL port is invalid")
		}
	}
	return URLValue{Type: "url", Profile: "url-1", Value: value}, nil
}
