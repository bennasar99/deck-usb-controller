cmake_minimum_required(VERSION 3.15)
project(deck2xinput CXX)

set(CMAKE_CXX_STANDARD 17)

add_executable(deck2xinput
    deck2xinput.cpp
    ViGEmClient/src/ViGEmClient.cpp
)

target_include_directories(deck2xinput PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/ViGEmClient/include
)

target_link_libraries(deck2xinput PRIVATE
    setupapi
    hid
    user32
)

if (MSVC)
    target_compile_options(deck2xinput PRIVATE /W4 /permissive-)
endif()
