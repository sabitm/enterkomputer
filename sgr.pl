#!/usr/bin/env perl
use strict;
use warnings;

my $lists = `sort -nr lists`;

if (! @ARGV) {
  print $lists;
} else {
  open my $grep, "|-", "grep -i @ARGV";
  print $grep $lists;
}
