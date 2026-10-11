#pragma once

#include <stdint.h>

#include <ansi.h>

//Codex: The reference ANSI codec assumes LP64 for 64-bit pointers; Windows uses LLP64.
//Codex: Override only its pointer macros, without changing vendored source or global ABI macros.
#undef UINTPTR_T
#undef INTPTR_T
#define UINTPTR_T uintptr_t
#define INTPTR_T intptr_t

#ifndef __in_ecount
#define __in_ecount(size)
#endif
#ifndef __out_ecount
#define __out_ecount(size)
#endif
#ifndef __in
#define __in
#endif
#ifndef __out
#define __out
#endif
