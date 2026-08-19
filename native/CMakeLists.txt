cmake_minimum_required(VERSION 3.16)
project(hoursx_sysd VERSION 0.1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# This binary holds privilege, so it is built with the hardening flags on by
# default rather than as an opt-in: stack protector, FORTIFY, full RELRO, and
# a position-independent executable.
add_compile_options(
  -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wsign-conversion
  -Wformat=2 -Wcast-qual -Wnon-virtual-dtor
  -fstack-protector-strong
  -fPIE
)
add_compile_definitions($<$<NOT:$<CONFIG:Debug>>:_FORTIFY_SOURCE=2>)
add_link_options(-pie -Wl,-z,relro -Wl,-z,now -Wl,-z,noexecstack)

# Shared implementation, linked into both the daemon and its test binary so the
# tests exercise the same code that ships.
add_library(hoursx_core STATIC
  src/policy.cpp
  src/protocol.cpp
  src/operations.cpp
)
target_include_directories(hoursx_core PUBLIC src)

add_executable(hoursx-sysd src/main.cpp)
target_link_libraries(hoursx-sysd PRIVATE hoursx_core)

enable_testing()
add_executable(hoursx-sysd-tests tests/test_policy.cpp)
target_link_libraries(hoursx-sysd-tests PRIVATE hoursx_core)
add_test(NAME policy COMMAND hoursx-sysd-tests)

install(TARGETS hoursx-sysd RUNTIME DESTINATION sbin)
