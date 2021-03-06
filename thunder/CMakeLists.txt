cmake_minimum_required(VERSION 3.0)
project(thunder)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_FLAGS "-Wall -Wextra -Werror -fPIC -pthread \
    -funroll-all-loops -O3 -march=native")

set(HEADER_LIBRARY src/include/randutils.hpp
                   src/include/tqdm.h
                   src/include/cxxpool.h
                   src/include/spline.h)

add_library(thunder src/anneal.cc src/anneal.hh src/util.cc
            src/util.hh src/detailed.cc src/detailed.hh
            src/multi_place.cc src/multi_place.hh
            src/global.cc src/global.hh
            src/vpr.cc src/vpr.hh
            ${HEADER_LIBRARY})

add_subdirectory(python/pybind11)
add_subdirectory(python)

add_executable(demo_test test.cc)
target_link_libraries(demo_test thunder)
